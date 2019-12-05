new_en, new_ts = [], []
with open("/Users/zacharypeng/Downloads/jw300.en") as en_file, open("/Users/zacharypeng/Downloads/jw300.ts") as ts_file:
    for en, ts in zip(en_file.readlines(), ts_file.readlines()):
        if len(en) == 0 and len(ts) == 0:
            continue
        if '~' in en or '~' in ts or '<' in en or '<' in ts:
            print(en)
            print(ts)
            continue
        if not en[0].isalpha() or not ts[0].isalpha():
            print(en)
            print(ts)
            continue
        new_en.append(en)
        new_ts.append(ts)

with open("/Users/zacharypeng/Downloads/new_jw300.en", 'w') as f:
    for line in new_en:
        f.write("%s" % line)

with open("/Users/zacharypeng/Downloads/new_jw300.ts", 'w') as f:
    for line in new_ts:
        f.write("%s" % line)
