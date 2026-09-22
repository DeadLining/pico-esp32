import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('export_pico', Path(__file__).parents[1] / 'export_pico_firmware.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ExportPicoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.build = self.root / 'build'
        (self.build / 'config').mkdir(parents=True)
        self.write('project_description.json', {'project_name':'pico','target':'esp32c3','project_version':'2.5.0'})
        self.write('config/sdkconfig.json', {'BOARD_TYPE_FOLOTOY_AI_PASSPORT':True,'ESPTOOLPY_FLASHSIZE_8MB':True})
        self.write('flasher_args.json', {'flash_files':{'0x10000':'pico.bin'}})
        (self.build / 'pico.bin').write_bytes(b'firmware')
        self.out = self.root / 'package'
    def write(self, name, data):
        (self.build / name).write_text(json.dumps(data))
    def test_exact_offsets_and_hash(self):
        m=module.export_package(self.build,self.out)
        self.assertEqual(m['files'][0]['address'],65536)
        self.assertEqual(len(m['files'][0]['sha256']),64)
        self.assertEqual((self.out/'pico.bin').read_bytes(),b'firmware')
        self.assertTrue((self.out/'pico-firmware.json').is_file())
    def test_wrong_board(self):
        self.write('config/sdkconfig.json', {'ESPTOOLPY_FLASHSIZE_8MB':True})
        with self.assertRaises(ValueError): module.export_package(self.build,self.out)
        self.assertFalse(self.out.exists())
    def test_traversal(self):
        (self.root/'outside.bin').write_bytes(b'bad')
        self.write('flasher_args.json', {'flash_files':{'0x0':'../outside.bin'}})
        with self.assertRaises(ValueError): module.export_package(self.build,self.out)
    def test_existing_output_not_overwritten(self):
        self.out.mkdir()
        with self.assertRaises(FileExistsError): module.export_package(self.build,self.out)
    def test_invalid_region(self):
        self.write('flasher_args.json', {'flash_files':{'0x800000':'pico.bin'}})
        with self.assertRaises(ValueError): module.export_package(self.build,self.out)
    def test_sector_overlap(self):
        (self.build/'other.bin').write_bytes(b'other')
        self.write('flasher_args.json', {'flash_files':{'0x10000':'pico.bin','65536':'other.bin'}})
        with self.assertRaises(ValueError): module.export_package(self.build,self.out)

if __name__ == '__main__': unittest.main()
