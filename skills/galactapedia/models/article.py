import json
import time

class Article:
    def __init__(self, data: dict = None) -> None:
        self.id = None
        self.title = None
        self.slug = None
        self.thumbnail = None
        self.type = None
        self.rsi_url = None
        self.api_url = None
        self.categories = []
        self.tags = []
        self.properties = []
        self.related_articles = []
        self.content = None
        self.created_at = None
        self.fetched_at = None

        if data:
            self.load_from_dict(data)

    def load_from_dict(self, data: dict) -> None:
        self.id = data.get("id", None)
        self.title = data.get("title", None)
        self.slug = data.get("slug", None)
        self.thumbnail = data.get("thumbnail", None)
        self.type = data.get("type", None)
        self.rsi_url = data.get("rsi_url", None)
        self.api_url = data.get("api_url", None)
        self.categories = data.get("categories", [])
        self.tags = data.get("tags", [])
        self.properties = data.get("properties", [])
        self.related_articles = data.get("related_articles", [])
        self.content = data.get("translations", None)
        self.created_at = data.get("created_at", None)
        self.fetched_at = data.get("fetched_at", time.strftime("%Y-%m-%d %H:%M:%S"))

    def __get_property_string(self) -> str:
        if not self.properties:
            return ""

        text = "Properties:\n"
        for property in self.properties:
            text += f"{property['name']}: {property['value']}\n"

    def __get_related_articles_string(self) -> str:
        if not self.related_articles:
            return ""

        text = "Related articles:\n"
        for article_teaser in self.related_articles:
            text += f"[{article_teaser['title']}]({article_teaser["url"]})\n"
        return text

    def __str__(self) -> str:
        return f"{self.title}\n\n{self.content}\n\n{self.__get_property_string()}\n{self.__get_related_articles_string()}"

    def get_as_string(self) -> str:
        return str(self)

    def get_as_json(self) -> str:
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)