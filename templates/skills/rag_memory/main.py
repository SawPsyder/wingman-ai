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
        self.withheld_messages = []
        self.ready = False
        self.new_message_counter = 0
        self.new_message_threshold = 10

    async def validate(self) -> list[WingmanInitializationError]:
        errors = await super().validate()

        if not self.wingman.openai:
            await self.wingman.validate_and_set_openai(errors)

        # setting model
        self.embedding_model = self.retrieve_custom_property_value("embedding_model", errors)

        # setting identifier
        self.rag_identifier = self.retrieve_custom_property_value("rag_identifier", errors).strip()
        self.rag_identifier = ''.join(filter(lambda x: not x.isalnum(), self.rag_identifier))
        if not self.rag_identifier:
            self.rag_identifier = self.wingman.config.name
        self.db_path = path.join(self.data_path, self.rag_identifier)

        return errors

    async def prepare(self) -> None:
        self.default_llm_call = self.wingman.actual_llm_call
        self.wingman.actual_llm_call = self.llm_call_with_rag

        self.threaded_execution(self.nested_prepare)

    async def nested_prepare(self) -> None:
        # chroma db setup and loading
        client_settings = Settings(anonymized_telemetry=False)
        client = chromadb.PersistentClient(path=self.db_path, settings=client_settings)
        embedding_func = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        self.vector_db = client.get_or_create_collection(
            name="rag_memory",
            embedding_function=embedding_func,
            metadata={"hnsw:space": "cosine"}
        )
        await self.printr.print_async(f"Memory loaded from {self.db_path}")
        self.ready = True
        await self.compress_snippets()
        await self.printr.print_async(f"Memory compressed")

    async def on_add_user_message(self, message: str) -> None:
        self.withheld_messages.append(message)

    async def on_add_assistant_message(self, message: str, tool_calls: list) -> None:
        self.withheld_messages.append(message)

    def get_message_timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def update_memory(self):
        for message in self.withheld_messages:
            self.new_message_counter += 1
            message = message.strip()
            if not message:
                continue

            print(f"########## Adding Message ({self.new_message_counter}) ##########")
            print(message)

            # add to memory
            timestamp = self.get_message_timestamp()
            self.vector_db.add(
                documents=[f"{timestamp} - {message}"],
                metadatas=[{"topic": "message", "timestamp": timestamp}],
                ids=[f"doc_{self.vector_db.count() + 1}"]
            )

        self.withheld_messages.clear()
        print(f"########## Updated Memory ({self.vector_db.count()}) ##########")
        for doc in self.vector_db.get()["documents"]:
            print(doc)

        if self.new_message_counter >= self.new_message_threshold:
            await self.compress_snippets()

    async def compress_snippets(self) -> list[str]:
        self.new_message_counter = 0
        snippets = self.vector_db.get()["documents"]

        # Use OpenAI to cluster and compress snippets
        prompt = (
            "You are given a list of messages. Your task is to merge the content of similar messages logically together. "
            "The snippets are unsorted, but the first part of each snippet is a timestamp. "
            "More recent snippets are more important and contain newer information. "
            "Return merged snippets without timestamps. "
            "Scrap unimportant information like greetings, farewells, and other irrelevant chatter. "
            "We are only interested in keeping facts and important information. "
            "Like names, preferences, or items in possession. "
            "We might also keep information about behavior adjustments, that should change the behavior of the assistant. "
            "Return the result in a json format without adding markdown or other formatting. Just pure JSON wiuthout keys."
            "\nHere are the snippets:\n\n" + "\n".join(f"- {snippet}" for snippet in snippets)
        )
        
        print("########## Prompt ##########")
        print(prompt)

        completion = await self.llm_call_with_rag(
            messages=[{"role": "system", "content": prompt}],
            tools=[],
            use_memory=False
        )
        original_answer = (
            completion.choices[0].message.content
            if completion and completion.choices
            else ""
        )
        
        print("########## Original Answer ##########")
        print(original_answer)

        try:
            answer = json.loads(original_answer)
            if isinstance(answer, list):
                snippets = answer
        except json.JSONDecodeError:
            print ("Error decoding JSON response from OpenAI")
            print (original_answer)
            return snippets

        print("########## New Snippets ##########")
        for snippet in snippets:
            print(snippet)

        # replace memory with consolidated snippets
        for doc_id in self.vector_db.get()["ids"]:
            self.vector_db.delete(ids=doc_id)

        for snippet in snippets:
            self.vector_db.add(
                documents=[f"{self.get_message_timestamp()} - {snippet}"],
                metadatas=[{"topic": "consolidated_memory"}],
                ids=[f"doc_{self.vector_db.count() + 1}"]
            )

        return snippets

    def _query_memory(self, queries, count=5):
        results = self.vector_db.query(
            query_texts=queries,
            n_results=count
        )
        return results['documents'][0]

    def _get_memory_chunks(self, messages) -> list[str]:
        queries = []
        max_queries = 3
        relevant_chunks = []

        for message in reversed(messages):
            if self.wingman.get_message_role(message) == "user":
                print(f"Adding query: {message.get('content', '')}")
                print(f"Role: {self.wingman.get_message_role(message)}")
                queries.append(message.get("content", ""))
                if len(queries) >= max_queries:
                    break

        if not queries:
            return relevant_chunks

        relevant_chunks = self._query_memory(queries)
        print(relevant_chunks)
        return relevant_chunks

    async def llm_call_with_rag(self, messages, tools: list[dict] = None, use_memory=True):
        if not self.ready:
            use_memory = False
        if use_memory and messages and self.wingman.get_message_role(messages[0]) == "system":
            print("Adding memory chunks")
            messages = messages.copy()
            memory_chunks = self._get_memory_chunks(messages)
            if memory_chunks:
                context_memory = "\n(BEGINNING of \"related_memory\")\n"+"\n".join(memory_chunks)+"\n(END of \"related_memory\")"
                print("########## Added to context ##########")
                print(context_memory)
                messages[0]["content"] += context_memory

        # prevents user messages beeing directly included in the memory
        if use_memory and self.withheld_messages:
            self.threaded_execution(self.update_memory)

        return await self.default_llm_call(messages, tools)
