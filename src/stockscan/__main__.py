"""Allow `python -m stockscan` to invoke the CLI."""
from stockscan.cli import app

if __name__ == "__main__":
    app()
