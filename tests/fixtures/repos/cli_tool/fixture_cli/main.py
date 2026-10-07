import argparse


def main():
    p = argparse.ArgumentParser(description="Convert registry exports to CSV")
    p.add_argument("path")
    print(p.parse_args().path)


if __name__ == "__main__":
    main()
