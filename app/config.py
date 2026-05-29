from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Railway provides DATABASE_URL automatically — use it directly
    DATABASE_URL: str = ""

    # Scraper settings
    OLX_BASE_URL: str = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/?currency=UZS"
    MAX_PAGES: int = 5
    SCRAPE_HOUR: int = 2
    SCRAPE_MINUTE: int = 0

    def get_async_db_url(self) -> str:
        # Railway gives postgresql:// — asyncpg needs postgresql+asyncpg://
        url = self.DATABASE_URL
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
