"""Download the free Dota 2, CS2, and Valorant player-lineup archives."""

import argparse
import json
import logging
import urllib.request
import zipfile
from pathlib import Path

from elo import esports_players

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fetch_esports_players")

DATASETS = {
    "dota2": {
        "url": "https://www.kaggle.com/api/v1/datasets/download/darianogina/dota-2-matches-pro-leagues",
        "archive": "dota2_matches_kaggle.zip",
        "member": "dota2_matches.parquet",
    },
    "valorant": {
        "url": ("https://www.kaggle.com/api/v1/datasets/download/"
                "ryanluong1/valorant-champion-tour-2021-2023-data"),
        "archive": "vct_2021_2026_kaggle_2026-06-26.zip",
    },
}

CS2_PARQUET_INDEX = "https://huggingface.co/api/datasets/blanchon/cs2_dataset_demo/parquet/default/train"


def _download(url: str, output: Path):
    request = urllib.request.Request(url, headers={"User-Agent": "divergence-bot/1.0"})
    temporary = output.with_suffix(output.suffix + ".part")
    with urllib.request.urlopen(request, timeout=120) as response, open(temporary, "wb") as handle:
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
    temporary.replace(output)


def fetch_dota(force: bool = False):
    title = "dota2"
    spec = DATASETS[title]
    directory = esports_players.DATA_DIRS[title]
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / spec["archive"]
    output = directory / spec["member"]
    if force or not archive.exists():
        log.info("Downloading %s bootstrap", title)
        _download(spec["url"], archive)
    if force or not output.exists():
        with zipfile.ZipFile(archive) as bundle:
            if spec["member"] not in bundle.namelist():
                raise RuntimeError(f"{archive} does not contain {spec['member']}")
            bundle.extract(spec["member"], directory)
    games = esports_players.load_games(title)
    log.info("%s audit: %s", title, esports_players.audit_games(games))


def fetch_cs2(force: bool = False):
    directory = esports_players.DATA_DIRS["cs2"]
    directory.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(CS2_PARQUET_INDEX,
                                     headers={"User-Agent": "divergence-bot/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        urls = json.load(response)
    if (not isinstance(urls, list) or not urls
            or any(not isinstance(url, str) or not url.startswith("https://") for url in urls)):
        raise RuntimeError("Hugging Face returned an invalid CS2 Parquet index")
    files = []
    for index, url in enumerate(urls):
        output = directory / f"{index}.parquet"
        files.append(output.name)
        if force or not output.exists():
            log.info("Downloading CS2 metadata shard %s/%s", index + 1, len(urls))
            _download(url, output)
    manifest = directory / "manifest.json"
    temporary = manifest.with_suffix(".json.part")
    temporary.write_text(json.dumps({"source": CS2_PARQUET_INDEX, "files": files}, indent=2),
                         encoding="utf-8")
    temporary.replace(manifest)
    games = esports_players.load_games("cs2")
    log.info("cs2 audit: %s", esports_players.audit_games(games))


def fetch_valorant(force: bool = False):
    spec = DATASETS["valorant"]
    directory = esports_players.DATA_DIRS["valorant"]
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / spec["archive"]
    if force or not archive.exists():
        log.info("Downloading Valorant VCT bootstrap")
        _download(spec["url"], archive)
    games = esports_players.load_games("valorant")
    log.info("valorant audit: %s", esports_players.audit_games(games))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("titles", nargs="*", choices=("dota2", "cs2", "valorant"),
                        default=("dota2", "cs2", "valorant"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    for requested in args.titles:
        {"dota2": fetch_dota, "cs2": fetch_cs2,
         "valorant": fetch_valorant}[requested](args.force)
