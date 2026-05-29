from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL — override via .env
    POSTGRES_USER: str = "olx_user"
    POSTGRES_PASSWORD: str = "olx_pass"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "olx_db"

    # Scraper settings
    OLX_BASE_URL: str = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/?currency=UZS"
    MAX_PAGES: int = 5
    SCRAPE_HOUR: int = 2    # 02:00 every day
    SCRAPE_MINUTE: int = 0

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
