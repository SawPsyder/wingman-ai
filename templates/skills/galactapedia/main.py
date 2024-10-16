import requests
import json
import asyncio
from models.article import Article
from api.interface import (
    WingmanInitializationError,
)
from skills.skill_base import Skill


class Galactapedia(Skill):

    def __init__(
        self,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.loaded = False
        self.fetched = False
        self.fetched_error = False
        self.api_url_galactapedia_overview = "https://api.star-citizen.wiki/api/v2/galactapedia"
        self.api_url_galactapedia_detail = "https://api.star-citizen.wiki/api/v2/galactapedia" # + ID
        self.api_retry_limit = 3
        self.api_timeout = 10
        self.api_locale = "en_EN" # en_EN, de_DE, zh_CN

        self.galactapedia_knowledgebase = {}
        self.galactapedia_articles = {}
        self.currently_fetched_article_ids = []
        self.galactapedia_categories = []

    async def validate(self) -> list[WingmanInitializationError]:
        errors = await super().validate()
        self.loaded = False
        self.fetched = False
        return errors

    async def unload(self) -> None:
        self.loaded = False
        await super().unload()

    async def prepare(self) -> None:
        self.loaded = True
        self.threaded_execution(self.fetch_galactapedia_knowledgebase)
        await super().prepare()

    async def is_summarize_needed(self, tool_name: str) -> bool:
        """Returns whether a tool needs to be summarized."""
        return True

    async def is_waiting_response_needed(self, tool_name: str) -> bool:
        """Returns whether a tool probably takes long and a message should be printed in between."""
        return False

    def get_tools(self) -> list[tuple[str, dict]]:
        if self.fetched:
            tools = [
                (
                    "use_galactapedia_knowledgebase",
                    {
                        "type": "function",
                        "function": {
                            "name": "use_galactapedia_knowledgebase",
                            "description": "Search the Galactapedia knowledgebase for a given subject. That should be just the name of the subject like 'FORENAME LASTNAME' or 'MANUFACTURER_NAME' or similar. If possible, also provide the type of information searched for.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "subject": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": self.galactapedia_categories,
                                    },
                                },
                                "required": ["subject"],
                                "optional": ["type"],
                            },
                        },

                    },
                ),
            ]
        elif self.fetched_error:
            tools = [
                (
                    "fetch_galactapedia_knowledgebase",
                    {
                        "type": "function",
                        "function": {
                            "name": "fetch_galactapedia_knowledgebase",
                            "description": "Fetches the Galactapedia knowledgebase data again. This may take a while.",
                            "parameters": {},
                        },
                    },
                ),
            ]
        else:
            tools = []
        return tools

    async def execute_tool(
        self, tool_name: str, parameters: dict[str, any]
    ) -> tuple[str, str]:
        function_response = ""
        instant_response = ""

        if self.settings.debug_mode:
            self.start_execution_benchmark()

        if tool_name == "use_galactapedia_knowledgebase":
            if "subject" in parameters and "type" in parameters:
                function_response = self.use_galactapedia_knowledgebase(parameters["subject"], parameters["type"])
            elif "subject" in parameters:
                function_response = self.use_galactapedia_knowledgebase(parameters["subject"])
            else:
                function_response = "Error: Missing parameter 'subject', automatically call again with correct parameters or ask user."
        elif tool_name == "fetch_galactapedia_knowledgebase":
            self.threaded_execution(self.fetch_galactapedia_knowledgebase)
            function_response = "Fetching Galactapedia knowledgebase data. This may take a moment."

        if self.settings.debug_mode:
            await self.printr.print_async(
                f"Perplexity answer: {function_response}"
            )
            await self.print_execution_time()

        return function_response, instant_response

    async def get_prompt(self) -> str | None:
        additional_prompt = None
        if not self.fetched and self.fetched_error:
            additional_prompt = "Important: If asked for information through the Galactapedia, please note that the data could not be fetched due to an error. You may ask the user if should be tried again."
        elif not self.fetched:
            additional_prompt = "Important: If asked for information through the Galactapedia, please note that the data is still being fetched. Please be patient."

        if additional_prompt is None:
            return await super().get_prompt()
        else:
            original_prompt = await super().get_prompt()
            return f"{original_prompt}\n\n{additional_prompt}"

    def use_galactapedia_knowledgebase(self, subject: str, type: str = None) -> str:
        if not self.fetched:
            return "Error: Galactapedia knowledgebase data not fetched yet. Please try again later."

        search_results = self.search_galactapedia_knowledgebase(subject, type)

        if not search_results:
            return f"Error: No information found for '{subject}' in the Galactapedia knowledgebase."

        if len(search_results) > 1:
            subject_options = []
            for article_id in search_results:
                self.threaded_execution(self.fetch_galactapedia_article, article_id, False, False) # preload
                subject_options.append(self.galactapedia_knowledgebase[article_id]["title"])
            return f"Found multiple subjects, please select. If selection is logic based on context, auto rerun function with correct subject: {', '.join(subject_options)}"

        article_id = search_results[0]
        if not self.fetch_galactapedia_article(article_id):
            return f"Error: Could not fetch article with ID {article_id} from the Galactapedia knowledgebase."

        article = self.galactapedia_articles[article_id]
        return str(article)

    def search_galactapedia_knowledgebase(self, subject: str, type: str = None) -> list:
        search_results = []
        for article_id, article in self.galactapedia_knowledgebase.items():
            if subject.lower() in article["title"].lower():
                if type:
                    if type in article["type"]:
                        search_results.append(article_id)
                else:
                    search_results.append(article_id)

        return search_results

    async def fetch_galactapedia_knowledgebase(self):
        self.galactapedia_knowledgebase = {}
        self.galactapedia_categories = []
        self.fetched = False
        self.fetched_error = False

        page = 0
        limit = 500
        max_entries = None

        if self.settings.debug_mode:
            await self.printr.print_async(
                "Loading Galactapedia knowledgebase...",
            )

        while max_entries is None or (page * limit) < max_entries:
            retry_limit = self.api_retry_limit
            data = None
            page += 1

            while retry_limit > 0 and not data:
                retry_limit -= 1
                try:
                    response = requests.get(
                        self.api_url_galactapedia_overview,
                        headers={
                            "accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        json={
                            "page": page,
                            "limit": limit,
                        },
                        timeout=self.api_timeout,
                    )
                    response.raise_for_status()
                    data = response.json()
                    if max_entries is None:
                        max_entries = data.get("meta", {}).get("total", 0)
                except requests.exceptions.RequestException as e:
                    await self.printr.print_async(
                        f"Error while retrieving data from {self.api_url_galactapedia_overview} page {page}: {e}"
                    )
                except json.decoder.JSONDecodeError as e:
                    await self.printr.print_async(
                        f"Error while decoding json from {self.api_url_galactapedia_overview} page {page}: {e}"
                    )

            if not data:
                await self.printr.toast_error(
                    f"Error while retrieving data from {self.api_url_galactapedia_overview} page {page}"
                )
                self.fetched_error = True
                break

            for entry in data.get("data", []):
                self.galactapedia_knowledgebase[entry["id"]] = entry
                for category in entry.get("type", "Unknown"):
                    if category not in self.galactapedia_categories:
                        self.galactapedia_categories.append(category)

        self.fetched = True

    async def fetch_galactapedia_article(self, article_id: str, force_fetch: bool = False, fetch_related: bool = False) -> bool:
        max_wait = self.api_retry_limit * self.api_timeout
        while article_id in self.currently_fetched_article_ids and max_wait > 0:
            max_wait -= 1
            await asyncio.sleep(1)

        if force_fetch or article_id not in self.galactapedia_articles:
            self.currently_fetched_article_ids.append(article_id)
            retry_limit = self.api_retry_limit
            data = None

            if self.settings.debug_mode:
                await self.printr.print_async(
                    f"Fetching Galactapedia article {article_id}...",
                )

            while retry_limit > 0 and data is None:
                retry_limit -= 1
                try:
                    response = requests.get(
                        f"{self.api_url_galactapedia_overview}/{article_id}",
                        headers={
                            "accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        json={
                            "locale": self.api_locale,
                        },
                        timeout=self.api_timeout,
                    )
                    response.raise_for_status()
                    data = response.json()
                except requests.exceptions.RequestException as e:
                    if self.settings.debug_mode:
                        await self.printr.print_async(
                            f"Error while retrieving data from {self.api_url_galactapedia_overview} article {article_id}: {e}"
                        )
                except json.decoder.JSONDecodeError as e:
                    if self.settings.debug_mode:
                        await self.printr.print_async(
                            f"Error while decoding json from {self.api_url_galactapedia_overview} article {article_id}: {e}"
                        )

            if not data:
                await self.printr.toast_error(
                    f"Error while retrieving data from {self.api_url_galactapedia_overview} article {article_id}"
                )
                self.currently_fetched_article_ids.remove(article_id)
                return False
            else:
                self.galactapedia_articles[article_id] = Article(data)

        self.currently_fetched_article_ids.remove(article_id)
        if not fetch_related:
            article = self.galactapedia_articles[article_id]
            for related_article in article.related_articles:
                if related_article["id"] not in self.currently_fetched_article_ids:
                    self.threaded_execution(self.fetch_galactapedia_article, related_article["id"], False, True)

        return True