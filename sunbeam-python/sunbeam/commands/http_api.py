import logging

import click
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from rich.console import Console

from sunbeam import log
from sunbeam.commands.bootstrap import run_bootstrap
from sunbeam.jobs.common import (
    Role
)

class BootstrapParams(BaseModel):
    accept_defaults: bool | None = None
    database: str = "auto"
    preseed_file: str | None = None
    roles: list[str] = ["control", "compute"]
    topology: str | None = None

LOG = logging.getLogger(__name__)
console = Console()

app = FastAPI()

@app.get("/")
async def read_root():
    return {"Hello": "World"}

@app.post("/cluster/bootstrap")
async def post_cluster_bootstrap(params: BootstrapParams):
    roles = []
    try:
        roles = [Role[role.upper()] for role in params.roles]
    except KeyError as e:
        raise LOG.error(f"Invalid cluster roles: {e}")
    await run_bootstrap(
        accept_defaults=params.accept_defaults,
        database=params.database,
        preseed=params.preseed_file,
        roles=roles,
        topology=params.topology,
        )
    return params

@click.command()
def start_server():
    config = uvicorn.Config(app, port=8000, log_level="info")
    server = uvicorn.Server(config)
    server.run() 