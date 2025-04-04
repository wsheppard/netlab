# We'll integrate Whoosh for indexing and searching the logs.
# We'll store the index in /tmp/<hash-of-logfile>.
# We no longer build by default. The user must explicitly pass --build to create/update the index.
# If searching is requested (e.g. --term or --around-ts) but the index doesn't exist, we error.
#
# We'll also show whether we found or created an index.
#
# We have removed 'raw' from the index. We store the entire 'data' field as TEXT(stored=True).
#
# Usage:
#   # Build index only:
#   python logtool.py --build /path/to/logs.zst
#
#   # Search by term (index must exist or we error):
#   python logtool.py --term "AP_UPDATE_DEVICE" /path/to/logs.zst
#
#   # Show logs around a timestamp (index must exist):
#   python logtool.py --around-ts 1680300000 /path/to/logs.zst

import argparse
import sys
import os
import hashlib
import json
import zstandard as zstd

from typing import Any
from netlab.eventbus import Event

# Whoosh imports
from whoosh.index import create_in, open_dir
from whoosh.fields import Schema, TEXT, ID, NUMERIC
from whoosh.qparser import QueryParser
from whoosh.query import NumericRange

from rich.console import Console

console = Console()
colors = ["red", "green", "blue", "cyan", "magenta", "yellow", "white", "bright_black"]

def guid_to_color(guid):
    return colors[hash(guid) % len(colors)]

# We'll keep the code that reads .zst lines in streaming fashion:
def read_zst_lines(path):
    with open(path, 'rb') as fh:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(fh) as reader:
            buffer = b""
            while True:
                chunk = reader.read(16384)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield line.decode('utf-8')
            if buffer:
                yield buffer.decode('utf-8')

# We'll define a schema for Whoosh:
schema = Schema(
    guid=ID(stored=True),       # We'll store as string ID
    name=TEXT(stored=True),     # We'll store the event's 'name'
    evtype=TEXT(stored=True),   # We'll store the event type
    data=TEXT(stored=True),     # We'll store the entire event.data as JSON text
    timestamp=NUMERIC(numtype=float, stored=True)  # We'll store the event.timestamp as float
)

def get_index_path(zst_path: str) -> str:
    """Compute /tmp/<hash-of-zst_path> as our Whoosh index location."""
    hashed = hashlib.md5(zst_path.encode('utf-8')).hexdigest()
    return f'/tmp/{hashed}'


def build_index(zst_path: str) -> None:
    """Create or open a Whoosh index at /tmp/<hash-of-logfile>, then index all logs in zst_path."""
    index_path = get_index_path(zst_path)
    new_index = False
    if not os.path.exists(index_path) or not os.listdir(index_path):
        os.makedirs(index_path, exist_ok=True)
        ix = create_in(index_path, schema)
        new_index = True
    else:
        ix = open_dir(index_path)

    writer = ix.writer()

    lines = read_zst_lines(zst_path)
    count = 0
    for line in lines:
        try:
            obj = json.loads(line)
            event = Event[Any].model_construct(**obj)

            # We'll store the entire event.data as JSON text
            data_dict = getattr(event, 'data', None)
            if data_dict is not None:
                data_json = json.dumps(data_dict, ensure_ascii=False)
            else:
                data_json = ""

            ts = float(getattr(event, 'timestamp', 0.0) or 0.0)

            writer.add_document(
                guid=str(event.guid),
                name=event.name or "",
                evtype=event.evtype or "",
                data=data_json,
                timestamp=ts
            )
            count += 1
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            print(f"Warning: failed to parse line: {e}", file=sys.stderr)

    writer.commit()
    if new_index:
        print(f"Created new index at {index_path}")
    else:
        print(f"Using existing index at {index_path}")
    print(f"Indexed {count} log entries")


def open_existing_index(zst_path: str):
    """Open an existing index for the given zst_path, or raise an error if missing."""
    index_path = get_index_path(zst_path)
    if not os.path.exists(index_path) or not os.listdir(index_path):
        print(f"No existing index at {index_path}.")
        build_index(zst_path)

    print(f"Using existing index at {index_path}")
    return open_dir(index_path)


def search_by_term(zst_path: str, query_str: str) -> None:
    """Search the 'data' field for query_str, requiring an existing index."""
    ix = open_existing_index(zst_path)
    with ix.searcher() as searcher:
        parser = QueryParser("data", schema=ix.schema)
        query = parser.parse(query_str)
        results = searcher.search(query, limit=None)
        print(f"Found {len(results)} results for query '{query_str}'")
        # for hit in results:
        #     print(
        #         f"guid={hit['guid']} "
        #         f"name={hit['name']} "
        #         f"evtype={hit['evtype']} "
        #         f"timestamp={hit['timestamp']} "
        #         f"data={hit['data']}"
        #     )
        for hit in sorted(results, key=lambda h: float(h["timestamp"])):
            color = guid_to_color(hit["guid"])
            line = (
                # f"guid={hit['guid']} "
                f"timestamp={hit['timestamp']} "
                f"name={hit['name']} "
                # f"evtype={hit['evtype']} "
                f"{hit['data']}"
            )
            console.print(line, style=color, highlight=False)


def search_around_timestamp(zst_path: str, around_ts: float, window: float = 5.0) -> None:
    """Show logs around a given timestamp +/- window, requiring an existing index."""
    ix = open_existing_index(zst_path)
    lower = around_ts - window
    upper = around_ts + window

    with ix.searcher() as searcher:
        q = NumericRange("timestamp", lower, upper, startexcl=False, endexcl=False)
        results = searcher.search(q, limit=None)
        print(f"Found {len(results)} results for timestamp range [{lower}, {upper}]")
        # sort results by ascending timestamp
        # for hit in sorted(results, key=lambda h: float(h["timestamp"])):
        #     print(
        #         f"name={hit['name']} "
        #         f"evtype={hit['evtype']} "
        #         f"timestamp={hit['timestamp']} "
        #         f"data={hit['data']}"
        #     )


        for hit in sorted(results, key=lambda h: float(h["timestamp"])):
            color = guid_to_color(hit["guid"])
            line = (
                # f"guid={hit['guid']} "
                f"timestamp={hit['timestamp']} "
                f"name={hit['name']} "
                # f"evtype={hit['evtype']} "
                f"{hit['data']}"
            )
            console.print(line, style=color, highlight=False)

def main():
    parser = argparse.ArgumentParser(description="LogTool with Whoosh integration.")
    parser.add_argument('--build', action='store_true', help="If provided, build/update the index for the specified log file.")
    parser.add_argument('--term', help="Search logs by substring in the 'data' field.")
    parser.add_argument('--around-ts', type=float, help="Show logs around this timestamp (+-5s by default).")
    parser.add_argument('--window', type=float, default=5.0, help="Window in seconds if using --around-ts.")

    parser.add_argument('zst_file', help="Path to .zst JSONL log file")

    args = parser.parse_args()

    if args.build:
        # Build/Update index only
        build_index(args.zst_file)
        return

    if args.term:
        # Search data field
        search_by_term(args.zst_file, args.term)
    elif args.around_ts is not None:
        # Search by timestamp range
        search_around_timestamp(args.zst_file, args.around_ts, args.window)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

