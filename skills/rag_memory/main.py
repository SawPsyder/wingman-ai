import openai
import chromadb
from chromadb.utils import embedding_functions
from api.interface import (
    WingmanInitializationError,
)
from skills.skill_base import Skill

class AskPerplexity(Skill):

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

    async def validate(self) -> list[WingmanInitializationError]:
        errors = await super().validate()

        if not self.wingman.openai:
            await self.wingman.validate_and_set_openai(errors)

        # setting model
        self.embedding_model = self.retrieve_custom_property_value("embedding_model", errors)

        # setting identifier
        self.rag_identifier = self.retrieve_custom_property_value("rag_identifier", errors)
        if not self.rag_identifier:
            self.rag_identifier = self.wingman.config.name

        return errors

    async def prepare(self) -> None:
        self.default_llm_call = self.wingman.actual_llm_call
        self.wingman.actual_llm_call = self.llm_call_with_rag
        
        # chroma db setup
        client = chromadb.Client()
        collection = client.get_collection(self.rag_identifier)

    def _get_embedding(self, query: str):
        response = openai.Embedding.create(input=query, model=self.embedding_model)
        return response["data"][0]["embedding"] or None

    def _get_memory_chunks(self, messages) -> list[str]:
        query = ""
        relevant_chunks = []

        for message in reversed(messages):
            if self.wingman.get_message_role(message) == "user":
                query = message.get("content", "")
                break

        if not query:
            return relevant_chunks

        query_embedding = self._get_embedding(query)
        if not query_embedding:
            return relevant_chunks

        relevant_chunks = self.vector_db.similarity_search(query_embedding, k=5)

        return query

    async def llm_call_with_rag(self, messages, tools: list[dict] = None):
        if messages and self.wingman.get_message_role == "systen":
            messages = messages.copy()
            messages[0]["text"] += "\n\n"+"\n".join(self._get_memory_chunks(messages))

        return self.default_llm_call(messages, tools)
