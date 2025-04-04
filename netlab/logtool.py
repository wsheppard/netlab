# We'll integrate Whoosh for indexing and searching the logs.
# We'll store the index in /tmp/<hash-of-logfile> and never let the user override it.
# If searching, we'll auto-build the index if it doesn't exist for the given log file.
#
# Usage:
#   # Index only:
#   python logtool.py /path/to/logs.zst
#
#   # Search (auto-build index if needed):
#   python logtool.py --term "AP_UPDATE_DEVICE" /path/to/logs.zst

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
from whoosh.fields import Schema, TEXT, ID, STORED
from whoosh.qparser import QueryParser


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
    guid=ID(stored=True),      # We'll store as string ID
    name=TEXT(stored=True),    # We'll store the event's 'name'
    evtype=TEXT(stored=True),  # We'll store the event type
    message=TEXT,              # We'll store the message if it exists in data
    raw=STORED                 # We'll store the full original JSON line
)


def get_index_path(zst_path: str) -> str:
    """Compute /tmp/<hash-of-zst_path> as our Whoosh index location."""
    hashed = hashlib.md5(zst_path.encode('utf-8')).hexdigest()
    return f'/tmp/{hashed}'


def build_index(zst_path: str) -> None:
    """Create or open a Whoosh index, then index all logs in zst_path."""
    index_path = get_index_path(zst_path)
    if not os.path.exists(index_path):
        os.makedirs(index_path)
        ix = create_in(index_path, schema)
    else:
        ix = open_dir(index_path)

    writer = ix.writer()

    lines = read_zst_lines(zst_path)
    count = 0
    for line in lines:
        try:
            # Zero-cost parse with pydantic model_construct
            obj = json.loads(line)
            event = Event[Any].model_construct(**obj)

            writer.add_document(
                guid=str(event.guid),
                name=event.name or "",
                evtype=event.evtype or "",
                message=json.dumps(event.data),
                raw=line
            )
            count += 1
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            print(f"Warning: failed to parse line: {e}", file=sys.stderr)

    writer.commit()
    print(f"Indexed {count} log entries into {index_path}")


def search_index(zst_path: str, query_str: str) -> None:
    """If index doesn't exist, build it, then search for query_str."""
    index_path = get_index_path(zst_path)
    if not os.path.exists(index_path):
        print(f"Index not found at {index_path}, building now...")
        build_index(zst_path)

    print(f"Index: {index_path}")

    ix = open_dir(index_path)
    with ix.searcher() as searcher:
        parser = QueryParser("message", schema=ix.schema)
        query = parser.parse(query_str)
        results = searcher.search(query, limit=None)
        print(f"Found {len(results)} results for query '{query_str}'")
        for hit in results:
            print(hit["raw"].strip())


def main():
    parser = argparse.ArgumentParser(description="LogTool with Whoosh integration.")
    parser.add_argument('--term', help="If provided, we will search logs by 'message' field.")
    parser.add_argument('zst_file', help="Path to .zst JSONL log file")

    args = parser.parse_args()

    if args.term:
        # Searching
        search_index(args.zst_file, args.term)

if __name__ == "__main__":
    main()

