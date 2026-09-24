import json,sys,os
d=json.load(sys.stdin)
print(json.dumps({'got':d,'secret':os.environ.get('CT_TEST_SECRET'),'cwd':os.getcwd()}))
