import json

def report():
    with open('coverage/arduino/coverage.json') as f:
        data = json.load(f)
    
    for f in data['files']:
        filename = f.get('file')
        # Filter as the shell script does
        if 'etl' in filename or 'wolfssl' in filename or 'wolfcrypt' in filename or 'rpc_protocol.h' in filename or 'rpc_structs.h' in filename:
            continue
        
        lines = f['lines']
        line_counts = {}
        for l in lines:
            ln = l['line_number']
            line_counts[ln] = line_counts.get(ln, 0) + l['count']
        
        f_total = len(line_counts)
        f_covered = len([ln for ln, count in line_counts.items() if count > 0])
        
        percent = (f_covered / f_total * 100) if f_total > 0 else 0
        
        uncovered = sorted([ln for ln, count in line_counts.items() if count == 0])
        print(f"{filename}: {percent:.1f}% ({f_covered}/{f_total})")
        if uncovered:
            print(f"  Uncovered: {uncovered[:20]}...")

if __name__ == '__main__':
    report()
