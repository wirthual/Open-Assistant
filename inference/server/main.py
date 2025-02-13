import asyncio
import json
import signal
import sys

import fastapi
import sqlmodel
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from oasst_inference_server import database, deps, models, plugins
from oasst_inference_server.models.fake_data_factories import (
    DbChatFactory,
    DbMessageFactory,
    DbUserFactory,
    DbWorkerFactory,
)
from oasst_inference_server.routes import account, admin, auth, chats, configs, workers
from oasst_inference_server.settings import settings
from oasst_shared.schemas import inference
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.middleware.sessions import SessionMiddleware

app = fastapi.FastAPI(title=settings.PROJECT_NAME)


# Allow CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.inference_cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Session middleware for authlib
app.add_middleware(SessionMiddleware, secret_key=settings.session_middleware_secret_key)


@app.middleware("http")
async def log_exceptions(request: fastapi.Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Exception in request")
        raise
    return response


# add prometheus metrics at /metrics
@app.on_event("startup")
async def enable_prom_metrics():
    Instrumentator().instrument(app).expose(app)


@app.on_event("startup")
async def log_inference_protocol_version():
    logger.warning(f"Inference protocol version: {inference.INFERENCE_PROTOCOL_VERSION}")


def terminate_server(signum, frame):
    logger.warning(f"Signal {signum}. Terminating server...")
    sys.exit(0)


@app.on_event("startup")
async def alembic_upgrade():
    signal.signal(signal.SIGINT, terminate_server)
    if not settings.update_alembic:
        logger.warning("Skipping alembic upgrade on startup (update_alembic is False)")
        return
    logger.warning("Attempting to upgrade alembic on startup")
    retry = 0
    while True:
        try:
            async with database.make_engine().begin() as conn:
                await conn.run_sync(database.alembic_upgrade)
            logger.warning("Successfully upgraded alembic on startup")
            break
        except Exception:
            logger.exception("Alembic upgrade failed on startup")
            retry += 1
            if retry >= settings.alembic_retries:
                raise

            timeout = settings.alembic_retry_timeout * 2**retry
            logger.warning(f"Retrying alembic upgrade in {timeout} seconds")
            await asyncio.sleep(timeout)
    signal.signal(signal.SIGINT, signal.SIG_DFL)


@app.on_event("startup")
async def maybe_add_debug_api_keys():
    debug_api_keys = settings.debug_api_keys_list
    if not debug_api_keys:
        logger.warning("No debug API keys configured, skipping")
        return
    try:
        logger.warning("Adding debug API keys")
        async with deps.manual_create_session() as session:
            for api_key in debug_api_keys:
                logger.info(f"Checking if debug API key {api_key} exists")
                if (
                    await session.exec(sqlmodel.select(models.DbWorker).where(models.DbWorker.api_key == api_key))
                ).one_or_none() is None:
                    logger.info(f"Adding debug API key {api_key}")
                    session.add(models.DbWorker(api_key=api_key, name="Debug API Key"))
                    await session.commit()
                else:
                    logger.info(f"Debug API key {api_key} already exists")
        logger.warning("Finished adding debug API keys")
    except Exception:
        logger.exception("Failed to add debug API keys")
        raise


# add routes
app.include_router(account.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(chats.router)
app.include_router(workers.router)
app.include_router(configs.router)

# mount plugins
for app_prefix, sub_app in plugins.plugin_apps.items():
    app.mount(path=settings.plugins_path_prefix + app_prefix, app=sub_app)


if settings.insert_fake_data:

    @app.on_event("startup")
    async def insert_fake_data_event():
        logger.warning("Inserting fake data into database (insert_fake_data is True)")
        async with deps.manual_create_session() as session:
            test_user_id = "test1"

            if (
                await session.exec(sqlmodel.select(models.DbUser).where(models.DbUser.id == test_user_id))
            ).one_or_none() is None:
                user_1 = DbUserFactory.build(
                    id=test_user_id,
                    display_name="testUserName1",
                    provider_account_id="debug",
                    deleted=False,
                    provider="debug",
                )
                session.add(user_1)
                await session.commit()
            else:
                logger.info(f"Fake user id {test_user_id} already exists.")

            worker_1 = DbWorkerFactory.build()
            session.add(worker_1)
            await session.commit()

            chat_1 = DbChatFactory.build(user_id=test_user_id)
            session.add(chat_1)
            await session.commit()

            with open(settings.fake_data_path) as f:
                dummy_messages_raw = json.load(f)

            messages = [
                DbMessageFactory.build(chat_id=chat_1.id, worker_id=worker_1.id, content=dm["text"], **dm)
                for dm in dummy_messages_raw
            ]
            session.add_all(messages)

            await session.commit()
        logger.warning("Done inserting fake data into database")


@app.on_event("startup")
async def welcome_message():
    logger.warning("Inference server started")
    logger.warning("To stop the server, press Ctrl+C")
