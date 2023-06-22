import logging

import click
import uvicorn
from fastapi import FastAPI
from rich.console import Console

from sunbeam import log

LOG = logging.getLogger(__name__)
console = Console()

app = FastAPI()

@app.get("/")
async def read_root():
    return {"Hello": "World"}

@app.post("/cluster/bootstrap")
async def post_cluster_bootstrap():
    return {"Cluster": "Bootstrap"}

@click.command()
def start_server():
    config = uvicorn.Config(app, port=8000, log_level="info")
    server = uvicorn.Server(config)
    server.run() 