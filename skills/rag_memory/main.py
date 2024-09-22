from os import path
import json
import chromadb
from chromadb.utils import embedding_functions
from chromadb.config import Settings
from services.file import get_writable_dir
from api.interface import (
    WingmanInitializationError,
)
from skills.skill_base import Skill
from datetime import datetime
import logging

class RagMemory(Skill):

    def __init__(
        self,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.embedding_model = None
        self.rag_identifier = None
        self.default_llm_call = None
        self.vector_db = None
        self.data_path = get_writable_dir(path.join("skills", "rag_memory", "data"))
        self.db_path = None
        self.temp_path = None
        self.withheld_messages = []
        self.ready = False
        self.batch_size = 10

        # Initialize logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)

    async def validate(self) -> list[WingmanInitializationError]:
        errors = await super().validate()

        if not self.wingman.openai:
            await self.wingman.validate_and_set_openai(errors)

        # Setting model
        self.embedding_model = self.retrieve_custom_property_value("embedding_model", errors)

        # Setting identifier
        self.rag_identifier = self.retrieve_custom_property_value("rag_identifier", errors).strip()
        # Keep only alphanumeric characters
        self.rag_identifier = ''.join(filter(lambda x: x.isalnum(), self.rag_identifier))
        if not self.rag_identifier:
            self.rag_identifier = self.wingman.config.name
        self.db_path = path.join(self.data_path, self.rag_identifier)
        self.temp_path = path.join(self.data_path, f"{self.rag_identifier}_temp_messages.json")

        return errors

    async def prepare(self) -> None:
        self.default_llm_call = self.wingman.actual_llm_call
        self.wingman.actual_llm_call = self.llm_call_with_rag

        self.threaded_execution(self.nested_prepare)

    async def nested_prepare(self) -> None:
        # Chroma DB setup and loading
        client_settings = Settings(anonymized_telemetry=False)
        client = chromadb.PersistentClient(path=self.db_path, settings=client_settings)

        # Use embedding model
        embedding_func = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        self.vector_db = client.get_or_create_collection(
            name="rag_memory",
            embedding_function=embedding_func,
            metadata={"hnsw:space": "cosine"}
        )
        await self.printr.print_async(f"Memory loaded from {self.db_path}")

        self.load_withheld_messages()

        self.ready = True
        
        if self.withheld_messages:
            await self.update_memory(True)
            await self.printr.print_async("Memory compressed")

    async def on_add_user_message(self, message: str) -> None:
        self.withheld_messages.append(message)
        self.save_withheld_messages()
        self.threaded_execution(self.update_memory)

    async def on_add_assistant_message(self, message: str, tool_calls: list) -> None:
        self.withheld_messages.append(message)
        self.save_withheld_messages()
        self.threaded_execution(self.update_memory)

    def get_message_timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def save_withheld_messages(self):
        with open(self.temp_path, 'w', encoding="UTF-8") as f:
            json.dump(self.withheld_messages, f)

    def load_withheld_messages(self):
        if path.exists(self.temp_path):
            with open(self.temp_path, 'r', encoding="UTF-8") as f:
                self.withheld_messages = json.load(f)
        else:
            self.withheld_messages = []

    async def update_memory(self, force_add=False):
        if not force_add and len(self.withheld_messages) < self.batch_size:
            return

        for message in self.withheld_messages:
            message = message.strip()
            if not message:
                continue

            # Add to memory
            timestamp = self.get_message_timestamp()
            self.vector_db.add(
                documents=[f"{timestamp} - {message}"],
                metadatas=[{"topic": "message", "timestamp": timestamp}],
                ids=[f"doc_{self.vector_db.count() + 1}"]
            )

        # Clear withheld messages before lengthy compression
        self.withheld_messages.clear()
        self.save_withheld_messages()

        await self.compress_snippets()

    async def compress_snippets(self) -> list[str]:
        # Get documents with topic 'message'
        messages = self.vector_db.get()["documents"]

        if not messages:
            return []

        # Use OpenAI to cluster and compress snippets
        prompt = (
            "You are given a list of messages. Your task is to merge similar messages logically together. "
            "So everything about one topic should be merged into one message for example the available money. "
            "For each merged message, add keywords the messages are about. "
            "These keywords should be diverse to function as search terms. "
            "Each message has a timestamp at the beginning to identify the newest message with up to date information. "
            "Scrap unimportant information, simple chatter and off topic messages as well as messages without facts. "
            "These messages are used as a memory database. Therefore the result should be a logical summary. "
            "Return the result as a JSON array where each element has 'message' and 'keywords'. "
            "Keep it in the language of the latest message. "
            "Do not include timestamps or any additional text. Just pure JSON without any markdown."
            "\nHere are the messages:\n\n" + "\n".join(f"- {message}" for message in messages)
        )

        completion = await self.llm_call_with_rag(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            use_memory=False
        )
        original_answer = (
            completion.choices[0].message.content
            if completion and completion.choices
            else ""
        )

        try:
            answer = json.loads(original_answer)
            if isinstance(answer, list):
                snippets = answer
            else:
                self.logger.error("Unexpected format in LLM response")
                self.logger.error(original_answer)
                return []
        except json.JSONDecodeError:
            self.logger.error("Error decoding JSON response from OpenAI")
            self.logger.error(original_answer)
            return []

        # replace memory with consolidated snippets
        for doc_id in self.vector_db.get()["ids"]:
            self.vector_db.delete(ids=doc_id)

        print("########## Consolidated snippets ##########")
        for item in snippets:
            message = item.get("message", "").strip()
            keywords = item.get("keywords", [])#
            print(f"Message: {message}")
            print(f"Keywords: {keywords}")
            print("")
            if not message:
                continue # possible issue as this risks losing data

            timestamp = self.get_message_timestamp()
            self.vector_db.add(
                documents=[f"{timestamp} - {message} - keywords: {''.join(keywords)}"],
                metadatas=[{"keywords": ' '.join(keywords), "timestamp": timestamp, "topic": "consolidated_memory"}],
                ids=[f"doc_{self.vector_db.count() + 1}"]
            )

        return [item.get("message", "") for item in snippets]

    async def _query_memory(self, queries, count=5):
        results = self.vector_db.query(
            query_texts=queries,
            n_results=count
        )
        return results['documents'][0] or []

    async def _get_memory_chunks(self, messages) -> list[str]:
        queries = []
        max_queries = 3
        relevant_chunks = []

        for message in reversed(messages):
            if self.wingman.get_message_role(message) == "user":
                queries.append(message.get("content", ""))
                if len(queries) >= max_queries:
                    break

        if not queries:
            return relevant_chunks

        relevant_chunks = await self._query_memory(queries)
        return relevant_chunks

    async def llm_call_with_rag(self, messages, tools: list[dict] = None, use_memory=True):
        if not self.ready:
            use_memory = False
        if use_memory and messages and self.wingman.get_message_role(messages[0]) == "system":
            messages = messages.copy()
            memory_chunks = await self._get_memory_chunks(messages)
            if memory_chunks:
                context_memory = "\n(BEGINNING of \"related_memory\")\n"+"\n".join(memory_chunks)+"\n(END of \"related_memory\")"
                print("########## Added to context ##########")
                print(context_memory)
                messages[0]["content"] += context_memory

        return await self.default_llm_call(messages, tools)
