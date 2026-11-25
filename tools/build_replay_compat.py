from pathlib import Path
import ctypes
import struct
import sys
from zipfile import ZIP_STORED, ZipFile


def main():
    game = Path(sys.argv[1])
    root = Path(__file__).resolve().parents[1]
    work = Path(sys.argv[3]) if len(sys.argv) > 3 else root / 'build' / 'release' / 'mods' / '2.3.1.2'
    work.mkdir(parents=True, exist_ok=True)
    module = sys.argv[2] if len(sys.argv) > 2 else 'oh2_replay_compat'
    source = root / 'src' / ('mod_' + module + '.py')
    dll = ctypes.PyDLL(str(game / 'win64' / 'python27.dll'))

    def api(name, result, *arguments):
        function = getattr(dll, name)
        function.restype = result
        function.argtypes = list(arguments)
        return function

    ctypes.c_int.in_dll(dll, 'Py_NoSiteFlag').value = 1
    ctypes.c_int.in_dll(dll, 'Py_IgnoreEnvironmentFlag').value = 1
    dll.Py_Initialize()
    pointer, string, integer = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
    virtual_source = ('scripts/client/gui/mods/mod_' + module + '.py').encode('ascii')
    compiler = api('Py_CompileString', pointer, string, string, integer)
    serialize = api('PyMarshal_WriteObjectToString', pointer, pointer, integer)
    as_bytes = api('PyString_AsString', pointer, pointer)
    length = api('PyString_Size', ctypes.c_ssize_t, pointer)
    code = compiler(source.read_bytes(), virtual_source, 257)
    if not code:
        dll.PyErr_Print()
        raise RuntimeError('Client Python rejected the source')
    payload = serialize(code, 2)
    if not payload:
        dll.PyErr_Print()
        raise RuntimeError('Could not serialize the client Python code')
    header = struct.pack('<II', dll.PyImport_GetMagicNumber(), int(source.stat().st_mtime))
    member = 'res/' + virtual_source.decode('ascii') + 'c'
    metadata = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<root><id>local.oh2_replay_compat</id>'
        '<version>1.0</version><name>OffHangar Replay Compatibility</name>'
        '<description>Offline replay settings and recorder lifecycle for 2.3.1.2</description></root>'
    )
    if module != 'oh2_replay_compat':
        metadata = '<root><id>local.%s</id><version>1.0</version><name>%s</name></root>' % (module, module)
    package = work / (module.replace('_', '-') + '.wotmod')
    with ZipFile(package, 'w', compression=ZIP_STORED) as archive:
        archive.writestr('meta.xml', metadata)
        archive.writestr(member, header + ctypes.string_at(as_bytes(payload), length(payload)))
    with ZipFile(package) as archive, ZipFile(game / 'res' / 'packages' / 'scripts.pkg') as base:
        assert archive.testzip() is None
        assert archive.read(member)[:4] == base.read('scripts/client/BattleReplay.pyc')[:4]
    print(package)


if __name__ == '__main__':
    main()
