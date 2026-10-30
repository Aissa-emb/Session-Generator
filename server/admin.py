"""Local administrator bootstrap; no unauthenticated remote bootstrap route."""
import argparse
import json
from .store import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['create-token'])
    parser.add_argument('--name',required=True)
    args=parser.parse_args()
    store=Store()
    try:
        print(json.dumps(store.create_token(args.name)))
    finally:
        store.engine.dispose()


if __name__=='__main__':
    main()
