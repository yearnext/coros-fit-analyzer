"""Original-FIT interchange and JSON HTTP contract tests; no external services."""
import base64
import hashlib
import struct
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.applications import Starlette
import server


def fit_fixture():
    # FIT record definition: timestamp (uint32), distance (uint32, 1/100 m).
    definition = bytes([0x40, 0, 0, 20, 0, 2, 253, 4, 0x86, 5, 4, 0x86])
    data = definition + b'\x00' + struct.pack('<II', 1000000000, 0) + b'\x00' + struct.pack('<II', 1000000010, 2000)
    body = struct.pack('<BBHI4s', 12, 0x10, 100, len(data), b'.FIT') + data
    crc = 0
    for value in body:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return body + struct.pack('<H', crc)


class OriginalFitTests(unittest.TestCase):
    def setUp(self):
        self.app = Starlette(routes=list(server.app.routes))
        self.cache = patch.dict(server._DATASETS, {}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_base64_preserves_original(self):
        raw = fit_fixture()
        parsed = server.parse_fit_file_base64('原始活动.fit', base64.b64encode(raw).decode())
        self.assertEqual(parsed['record_count'], 2)
        self.assertIn('export_fit_original', parsed['available_followups'])
        descriptor = server.export_fit_original(parsed['dataset_id'])
        self.assertEqual(descriptor['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(descriptor['file_bytes'], len(raw))
        with TestClient(self.app) as client:
            response = client.get(descriptor['download_path'])
        self.assertEqual(response.content, raw)
        self.assertEqual(response.headers['content-type'], 'application/vnd.ant.fit')
        self.assertIn("filename*=UTF-8''", response.headers['content-disposition'])

    def test_binary_upload_json(self):
        raw = fit_fixture()
        with TestClient(self.app) as client:
            response = client.post('/analyze/fit?file_name=activity.fit',content=raw,
                                   headers={'Content-Type':'application/octet-stream'})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result['parse']['record_count'], 2)
            self.assertEqual(result['facts']['dataset_id'], result['parse']['dataset_id'])
            self.assertEqual(result['facts']['overview']['distance_m']['value'], 20)
            self.assertEqual(client.get(result['original_fit']['download_path']).content, raw)

    def test_invalid_uploads(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.post('/analyze/fit',json={}).status_code,415)
            for content in (b'', b'invalid fit', fit_fixture()[:-1]+b'\xff'):
                response=client.post('/analyze/fit',content=content,headers={'Content-Type':'application/vnd.ant.fit'})
                self.assertEqual(response.status_code,400)
                self.assertIn('error',response.json())
        self.assertEqual(len(server._DATASETS),0)

    def test_size_limit(self):
        with patch.object(server,'MAX_FIT_BYTES',8), TestClient(self.app) as client:
            response=client.post('/analyze/fit',content=b'123456789',headers={'Content-Type':'application/octet-stream'})
        self.assertEqual(response.status_code,413)

    def test_expired_download(self):
        parsed=server._decode_fit(fit_fixture(),'activity.fit')
        path=server.export_fit_original(parsed['dataset_id'])['download_path']
        server._DATASETS[parsed['dataset_id']]['created']-=server.CACHE_TTL_SECONDS+1
        with TestClient(self.app) as client:
            response=client.get(path)
        self.assertEqual(response.status_code,404)
        self.assertIn('error',response.json())

    def test_url_input_preserves_same_bytes(self):
        raw=fit_fixture()
        with patch.object(server,'_download_fit',return_value=raw):
            parsed=server.parse_fit_file_url('https://example.coros.com/activity.fit')
        self.assertEqual(server._get_dataset(parsed['dataset_id'])['raw_fit'],raw)


if __name__=='__main__': unittest.main()

