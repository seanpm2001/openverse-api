#!/usr/bin/env python
import csv
import json
import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from decouple import config
from python_on_whales import DockerException, docker


log_level = config("LOG_LEVEL", default="INFO").upper()
logging.basicConfig(level=log_level)


#############
# Constants #
#############


WEB_SERVICE_NAME = config("WEB_SERVICE_NAME", default="web")
CACHE_SERVICE_NAME = config("CACHE_SERVICE_NAME", default="cache")
UPSTREAM_DB_SERVICE_NAME = config("UPSTREAM_DB_SERVICE_NAME", default="upstream_db")
DB_SERVICE_NAME = config("DB_SERVICE_NAME", default="db")

MEDIA_TYPES = ["image", "audio"]
MediaType = Literal["image", "audio"]


##############
# Subscripts #
##############


PSQL = "psql -U deploy -d openledger -v ON_ERROR_STOP=1 -X"
DJ_SHELL = "python manage.py shell"


##########
# Models #
##########


@dataclass
class Provider:
    identifier: str
    name: str
    url: str
    media_type: str
    filter_content: bool = False

    @property
    def sql_value(self) -> str:
        fields = ", ".join(
            [
                "now()",
                f"'{self.identifier}'",
                f"'{self.name}'",
                f"'{self.url}'",
                f"'{self.media_type}'",
                str(self.filter_content).lower(),
            ]
        )
        return f"({fields})"


@dataclass
class Column:
    name: str
    type: str


###########
# Helpers #
###########


def compose_exec(service: str, bash_input: str) -> str:
    """
    Run the given input inside a Bash shell inside the container.

    :param service: the name of the service inside which to execute the commands
    :param bash_input: the input for the Bash shell
    :return: the output of the operation
    :raise: ``DockerException`` if the command fails during execution
    """

    bash_input = re.sub(r"\n\s{8}", r"\n", bash_input)
    logging.debug(f"Bash input: {bash_input}")
    output = docker.compose.execute(service, ["/bin/bash", "-c", bash_input], tty=False)
    logging.debug(f"Docker output: {output}")
    return output


def copy_table_upstream(
    name: str, target_name: str = None, delete_if_exists: bool = True
) -> str:
    """
    Copy the given table from the downstream DB to the upstream DB. Any existing table
    with the same name can be deleted before copying and the table can be renamed after
    copying.

    :param name: the name of the source table to copy
    :param target_name: the name to assign to the copied table
    :param delete_if_exists: whether to delete any existing tables with the target name
    """

    target_name = target_name or name

    logging.info(f"Copying table '{name}' to '{target_name}'...")

    copy = (
        "PGPASSWORD=deploy "
        f"pg_dump -s -t {name} -U deploy -d openledger -h {DB_SERVICE_NAME} | "
        f"{PSQL}"
    )
    delete = f"DROP TABLE IF EXISTS {target_name} CASCADE;" if delete_if_exists else ""
    rename = (
        f"ALTER TABLE {name} RENAME TO {target_name}" if target_name != name else ""
    )

    bash_input = f"""{PSQL} <<EOF
        {delete}
        EOF
        {copy}
        {PSQL} <<EOF
        {rename}
        EOF"""
    output = compose_exec(UPSTREAM_DB_SERVICE_NAME, bash_input)
    logging.info(f"Table '{name}' copied to '{target_name}'.")
    return output


def run_just(recipe: str, argv: list[str]) -> subprocess.CompletedProcess:
    """
    Run the given ``just`` recipe with the given arguments.

    :param recipe: the name of the ``just`` recipe to invoke
    :param argv: the list of arguments to pass after the ``just`` recipe
    :return: the process obtained from the ``subprocess.run`` command
    """

    try:
        logging.debug(f"just {recipe} {' '.join(argv)}")
        proc = subprocess.run(
            ["just", recipe, *argv],
            check=True,
            capture_output=True,
            text=True,
        )
        logging.debug(f"Output: {proc.stdout}")
        return proc
    except subprocess.CalledProcessError as exc:
        logging.error("Just call failed.")
        logging.error(f"STDOUT: {exc.stdout}")
        logging.error(f"STDERR: {exc.stderr}")
        raise


def get_actual_providers() -> list[str]:
    """
    Get the list of all distinct providers mentioned in the sample data.

    :return: the list of unique providers in the sample data
    """

    providers = set()
    for media_type in MEDIA_TYPES:
        sample_file_path = f"./sample_data/sample_{media_type}.csv"
        with open(sample_file_path, "r") as sample_file:
            reader = csv.DictReader(sample_file)
            for row in reader:
                providers.add(row["provider"])
    return list(providers)


#########
# Steps #
#########


def run_migrations():
    """
    Run all migrations for the API.
    """

    logging.info("Executing migrations...")
    bash_input = "python manage.py migrate --noinput"
    compose_exec(WEB_SERVICE_NAME, bash_input)
    logging.info("Migrations executed.")


def create_users(names: list[str]):
    """
    Create users with the given usernames in the API database. The password for the
    users is always set to "deploy". Users that already exist will not be recreated.
    """

    logging.info("Creating users...")
    bash_input = f"""{DJ_SHELL} <<EOF
        from django.contrib.auth.models import User
        usernames = {names}
        for username in usernames:
            if User.objects.filter(username=username).exists():
                print(f'User {{username}} already exists')
                continue
            if username == 'deploy':
                user = User.objects.create_superuser(
                    username, f'{{username}}@example.com', 'deploy'
                )
            else:
                user = User.objects.create_user(
                    username, f'{{username}}@example.com', 'deploy'
                )
                user.save()
        EOF"""
    compose_exec(WEB_SERVICE_NAME, bash_input)
    logging.info("Users created.")


def backup_table(media_type: MediaType):
    """
    Create a backup of the table created for the given media type. This table will be
    free of the modifications made during ingestion and thereby allow making the ingest
    step idempotent.

    :param media_type: the media type whose table is being backed up
    """

    logging.info(f"Backing up '{media_type}' table...")
    bash_input = f"""{PSQL} <<EOF
        CREATE TABLE {media_type}_template
            (LIKE {media_type} INCLUDING ALL);
        CREATE SEQUENCE {media_type}_template_id_seq;
        ALTER TABLE {media_type}_template
            ALTER COLUMN id
            SET DEFAULT nextval('{media_type}_template_id_seq');
        ALTER SEQUENCE {media_type}_template_id_seq
            OWNED BY {media_type}_template.id;
        EOF"""
    try:
        compose_exec(DB_SERVICE_NAME, bash_input)
        logging.info(f"Backed up '{media_type}' table to '{media_type}_template'.")
    except DockerException as exc:
        if f'relation "{media_type}_template" already exists' in exc.stderr:
            logging.warning(f"Backup table '{media_type}_template' already exists.")
            # Do nothing if the error was caused by an existing backup
        else:
            raise


def load_content_providers(providers: list[Provider]):
    """
    Load the given providers into the database. The given providers will be removed from
    the database, if they exist, and then re-added.

    :param providers: the list of providers to load
    """

    logging.info(f"Creating {len(providers)} providers...")

    identifiers = ", ".join([f"'{provider.identifier}'" for provider in providers])
    values = ", ".join([provider.sql_value for provider in providers])

    bash_input = f"""{PSQL} <<EOF
        DELETE FROM content_provider
            WHERE provider_identifier IN ({identifiers});
        INSERT INTO content_provider
            (created_on,provider_identifier,provider_name,domain_name,media_type,filter_content)
        VALUES
            {values};
        EOF"""
    compose_exec(DB_SERVICE_NAME, bash_input)
    logging.info(f"Created {len(providers)} providers.")


def load_sample_data(media_type: MediaType, extra_columns: list[Column] = None):
    """
    Copy data from the sample data files into the upstream DB tables. Any extra columns
    required can be added to the table.

    :param media_type: the name of the model to copy sample data for
    :param extra_columns: the list of additional columns to create on the table
    """

    logging.info(f"Loading sample data for media type '{media_type}'...")

    source_table = f"{media_type}_template"
    dest_table = f"{media_type}_view"
    copy_table_upstream(source_table, dest_table)

    add = ""
    if extra_columns:
        add_directives = ", ".join(
            [f"ADD COLUMN {column.name} {column.type}" for column in extra_columns]
        )
        add = f"ALTER TABLE {dest_table} {add_directives};"

    sample_file_path = f"./sample_data/sample_{media_type}.csv"
    with open(sample_file_path, "r") as sample_file:
        columns = sample_file.readline().strip()
        logging.debug(f"CSV columns: {columns}")
    copy = (
        f"\\copy {dest_table} ({columns}) from '{sample_file_path}' "
        "with (FORMAT csv, HEADER true);"
    )

    bash_input = f"""{PSQL} <<EOF
        {add}
        {copy}
        EOF"""
    compose_exec(UPSTREAM_DB_SERVICE_NAME, bash_input)
    logging.info(f"Sample data for media type '{media_type}' loaded.")


def create_audioset_view():
    """
    Create the ``audioset_view`` view from the ``audio_view`` table by breaking the
    ``audio_set`` JSONB field into its constituent keys as separate columns.
    """

    logging.info("Creating audio set view...")

    columns = [
        Column("foreign_identifier", "varchar(1000)"),
        Column("title", "varchar(2000)"),
        Column("foreign_landing_url", "varchar(1000)"),
        Column("creator", "varchar(2000)"),
        Column("creator_url", "varchar(2000)"),
        Column("url", "varchar(1000)"),
        Column("filesize", "integer"),
        Column("filetype", "varchar(80)"),
        Column("thumbnail", "varchar(1000)"),
    ]
    select_directives = ", ".join(
        [
            f"(audio_set ->> '{column.name}') :: {column.type} as {column.name}"
            for column in columns
        ]
    )

    bash_input = f"""{PSQL} <<EOF
        UPDATE audio_view
            SET audio_set_foreign_identifier = audio_set ->> 'foreign_identifier';
        DROP VIEW IF EXISTS audioset_view;
        CREATE VIEW audioset_view
        AS
            SELECT DISTINCT
                {select_directives},
                provider
            FROM audio_view
            WHERE audio_set IS NOT NULL;
        EOF"""
    compose_exec(UPSTREAM_DB_SERVICE_NAME, bash_input)
    logging.info("Audio set view created.")


def ingest(media_type: MediaType):
    """
    Create test data and actual indices for the given media type. New indices are
    created in each run so repeatedly running may fill up ES to maximum capacity.

    :param media_type: the media type for which to create ES indices
    """

    logging.info(f"Loading test data for media type '{media_type}'...")
    run_just("load-test-data", [media_type])
    time.sleep(2)  # seconds
    logging.info("Test data loaded.")

    logging.info(f"Getting current index status for media type '{media_type}'...")
    proc = run_just("stat", [media_type])
    data = json.loads(proc.stdout)
    logging.debug(f"Stat response: {data}")
    logging.info("Current index status fetched.")

    suffix = uuid.uuid4().hex
    logging.debug(f"New index suffix: {suffix}")

    # TODO: Find the cause of flaky image ingestion.
    retries = 2 if media_type == "image" else 0
    while True:
        try:
            logging.info("Running data ingestion...")
            run_just("ingest-upstream", [media_type, suffix])
            run_just("wait-for-index", [f"{media_type}-{suffix}"])
            logging.info("Data ingestion completed.")
            break
        except subprocess.CalledProcessError:
            if not retries:
                logging.critical("Failed and no retries left. Crashing.")
                raise
            print(f"Failed but {retries} retries left. Re-attempting.")
            retries -= 1

    logging.info(f"Promoting index '{media_type}-{suffix}'...")
    run_just("promote", [media_type, suffix, media_type])
    run_just("wait-for-index", [media_type])
    logging.info(f"Index '{media_type}-{suffix}' promoted.")

    if data["exists"]:
        old_suffix = data["alt_names"].lstrip(f"{media_type}-")
        logging.info(f"Deleting old index '{media_type}-{old_suffix}'...")
        run_just("delete-index", [media_type, old_suffix])
        logging.info("Old index deleted.")


if __name__ == "__main__":
    # API initialisation
    logging.info("\n---\nAPI initialisation\n---\n")
    run_migrations()
    create_users(["deploy", "continuous_integration"])
    for media_type in MEDIA_TYPES:
        backup_table(media_type)

    providers = {
        "flickr": Provider("flickr", "Flickr", "https://www.flickr.com", "image"),
        "stocksnap": Provider(
            "stocksnap", "StockSnap", "https://stocksnap.io", "image"
        ),
        "freesound": Provider(
            "freesound", "Freesound", "https://freesound.org/", "audio"
        ),
        "jamendo": Provider("jamendo", "Jamendo", "https://www.jamendo.com", "audio"),
        "wikimedia_audio": Provider(
            "wikimedia_audio", "Wikimedia", "https://commons.wikimedia.org", "audio"
        ),
        "thingiverse": Provider(
            "thingiverse", "Thingiverse", "https://www.thingiverse.com", "model_3d"
        ),
    }
    logging.debug(f"Total providers: {len(providers)}")
    actual_providers = get_actual_providers()
    logging.debug(f"Used providers: {len(actual_providers)}")
    load_content_providers([providers[provider] for provider in actual_providers])

    # Upstream initialisation
    logging.info("\n---\nUpstream initialisation\n---\n")
    copy_table_upstream("content_provider")

    standardized_popularity = Column("standardized_popularity", "double precision")
    ingestion_type = Column("ingestion_type", "varchar(1000)")
    audio_set = Column("audio_set", "jsonb")
    extra_columns = {
        "image": [standardized_popularity, ingestion_type],
        "audio": [standardized_popularity, ingestion_type, audio_set],
    }
    for media_type in MEDIA_TYPES:
        load_sample_data(media_type, extra_columns[media_type])

    create_audioset_view()

    # Data refresh
    logging.info("\n---\nData refresh\n---\n")
    for media_type in MEDIA_TYPES:
        ingest(media_type)

    # Cache bust
    logging.info("\n---\nCache bust\n---\n")
    for media_type in MEDIA_TYPES:
        logging.info(f"Busting cache for media type '{media_type}'...")
        compose_exec(
            CACHE_SERVICE_NAME, f'echo "del :1:sources-{media_type}" | redis-cli'
        )
        logging.info("Cache busted.")