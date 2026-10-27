from alembic import context

from backend.models import Base

connection = context.config.attributes.get("connection")
if connection is None:
    from backend.config import Settings
    from sqlalchemy import create_engine

    settings = Settings()
    settings.prepare()
    with create_engine(settings.database_url).connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
else:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
