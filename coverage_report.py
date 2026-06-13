import json


def report():
    with open("coverage/arduino/coverage.json") as coverage_file:
        data = json.load(coverage_file)

    for file_entry in data["files"]:
        filename = file_entry.get("file")
        # Filter as the shell script does
        if (
            "etl" in filename
            or "wolfssl" in filename
            or "wolfcrypt" in filename
            or "rpc_protocol.h" in filename
            or "rpc_structs.h" in filename
        ):
            continue

        lines = file_entry["lines"]
        line_counts = {}
        for line in lines:
            ln = line["line_number"]
            line_counts[ln] = line_counts.get(ln, 0) + line["count"]

        f_total = len(line_counts)
        f_covered = len([ln for ln, count in line_counts.items() if count > 0])

        percent = (f_covered / f_total * 100) if f_total > 0 else 0

        uncovered = sorted([ln for ln, count in line_counts.items() if count == 0])
        print(f"{filename}: {percent:.1f}% ({f_covered}/{f_total})")
        if uncovered:
            print(f"  Uncovered: {uncovered[:20]}...")


if __name__ == "__main__":
    report()
