import py_compile
import os
import sys
root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
errors = []
for dirpath, dirnames, filenames in os.walk(root):
    for f in filenames:
        if f.endswith('.py'):
            path = os.path.join(dirpath, f)
            try:
                py_compile.compile(path, doraise=True)
            except Exception as e:
                errors.append((path, str(e)))

if errors:
    print('COMPILATION_ERRORS:')
    for p, msg in errors:
        print(p, msg)
    sys.exit(1)
else:
    print('ALL_PY_COMPILE_OK')
