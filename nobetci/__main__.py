from .app import App
from .config import Config

if __name__ == "__main__":
    App(Config.from_env()).run()
