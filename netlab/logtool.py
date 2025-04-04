import argparse
import json
import sys
from typing import Any
import uuid
import zstandard as zstd
from netlab.eventbus import Event

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

def extract_info(lines):
    from collections import defaultdict

    seen_guids = defaultdict(lambda: {"name": None, "count": 0})
    from collections import defaultdict
    event_types = defaultdict(int)
    count = 0
    for line in lines:
        try:
            obj = json.loads(line)
            event = Event[Any].model_construct(**obj)
            key = str(event.guid)
            seen_guids[key]["name"] = event.name
            seen_guids[key]["count"] += 1
            event_types[event.evtype] += 1
            count += 1
        except Exception as e:
            print(f"Warning: failed to parse line: {e}", file=sys.stderr)
    return seen_guids, event_types, count

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file', help="Path to .zst JSONL log file")
    args = parser.parse_args()

    lines = read_zst_lines(args.file)
    guids, evtypes, count = extract_info(lines)

    print(f"\nTotal log entries parsed: {count}\n")

    print(f"== Unique GUIDs ({len(guids)}) ==")
    for guid, data in sorted(guids.items(), key=lambda item: item[1]["count"], reverse=True):
        print(f"{guid} -> {data['name']} ({data['count']})")

    print(f"\n== Unique Event Types ({len(evtypes)}) ==")
    for ev, count in sorted(evtypes.items(), key=lambda item: item[1], reverse=True):
        print(f"{ev} ({count})")

if __name__ == "__main__":
    main()

