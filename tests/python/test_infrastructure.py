from support import *


class InfrastructureTests(unittest.TestCase):
    def test_r2_list_all_parses_and_pages_s3_xml(self):
            client = R2Client("https://<account-id>.r2.cloudflarestorage.com", "<bucket-name>", "key", "secret")
            calls = []
            pages = [
                b'''<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Contents><Key>one</Key><ETag>&quot;e1&quot;</ETag><Size>3</Size><LastModified>2023-11-14T22:13:20.000Z</LastModified></Contents><IsTruncated>true</IsTruncated><NextContinuationToken>next-token</NextContinuationToken></ListBucketResult>''',
                b'''<ListBucketResult><Contents><Key>two</Key><ETag>e2</ETag><Size>4</Size></Contents><IsTruncated>false</IsTruncated></ListBucketResult>''',
            ]

            def fake_request(method, key, query):
                calls.append((method, key, query))
                return pages[len(calls) - 1], None

            client._request = fake_request
            self.assertEqual(client.list_all("prefix/"), [
                {"key": "one", "etag": '"e1"', "size": 3, "lastModified": "2023-11-14T22:13:20.000Z"},
                {"key": "two", "etag": "e2", "size": 4, "lastModified": None},
            ])
            self.assertEqual(calls[0][2], {"list-type": "2", "prefix": "prefix/"})
            self.assertEqual(calls[1][2], {"list-type": "2", "prefix": "prefix/", "continuation-token": "next-token"})

    def test_aes_and_filename_roundtrip(self):
            key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
            plain = bytes.fromhex("00112233445566778899aabbccddeeff")
            self.assertEqual(_aes_block(plain, key).hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")
            self.assertEqual(_aes_block(_aes_block(plain, key), key, True), plain)

            cipher = RcloneBase64("test-password")
            name = "深い階層/note 日本語.md"
            self.assertEqual(cipher.decrypt_path(cipher.encrypt_path(name)), name)

    def test_rclone_content_roundtrip_and_authentication(self):
            cipher = RcloneBase64("test-password")
            node_vector = bytes.fromhex(
                "52434c4f4e450000000102030405060708090a0b0c0d0e0f"
                "1011121314151617"
                "1d840608f1e4c1ee2dc861e09e08b19eca3219482673c21456f82d579c4e62e60c6f452aff0b"
            )
            self.assertEqual(cipher.decrypt_content(node_vector), bytes.fromhex("e697a5e69cace8aa9ee381ae4d61726b646f776e5c6e"))
            clear = ("日本語のMarkdown\n" * 20).encode("utf-8")
            encrypted = encrypt_content_for_test(clear, cipher, bytes(range(24)))
            self.assertEqual(cipher.decrypt_content(encrypted), clear)
            self.assertEqual(cipher.decrypt_content(cipher.encrypt_content(clear)), clear)
            tampered = bytearray(encrypted)
            tampered[-1] ^= 1
            with self.assertRaises(Exception):
                cipher.decrypt_content(bytes(tampered))

    def test_rclone_filename_components_are_cached(self):
            cipher = RcloneBase64("test-password")
            encrypted = cipher.encrypt_path("shared/note.md")

            with mock.patch.object(crypto_module, "_eme_transform", wraps=crypto_module._eme_transform) as transform:
                self.assertEqual(cipher.decrypt_path(encrypted), "shared/note.md")
                first_call_count = transform.call_count
                self.assertGreater(first_call_count, 0)
                self.assertEqual(cipher.decrypt_path(encrypted), "shared/note.md")
                self.assertEqual(transform.call_count, first_call_count)

    def test_filename_matches_node_vector(self):
            cipher = RcloneBase64("test-password")
            self.assertEqual(cipher.encrypt_path("深い階層/note 日本語.md"), "-onHdzFTWqw_CLt0KW16rw/GcYc3DaSITf6jaTJoLooEyruftqJY7KDGYnUwQy3MrA")

    def test_apply_is_atomic_and_restores_mtime(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                state = Path(root) / "app-data" / "state.json"
                remote = FakeRemote({"note": RemoteObject("新しい内容\n".encode(), '"etag-1"', {"mtime": "1700000000"})})
                result = execute_probe(vault, state, [{"path": "深い/note.md", "key": "note"}], remote.get, PlainContent(), apply=True)
                self.assertTrue(result["ok"])
                target = vault / "深い" / "note.md"
                self.assertEqual(target.read_text(encoding="utf-8"), "新しい内容\n")
                self.assertAlmostEqual(target.stat().st_mtime, 1700000000, delta=0.01)
                entries = load_state(state)
                self.assertEqual(entries["深い/note.md"]["remoteETag"], '"etag-1"')
                self.assertEqual(entries["深い/note.md"]["localContentHash"], hashlib.sha256(target.read_bytes()).hexdigest())
                self.assertFalse(any(path.name.startswith(".r2-sync-") for path in target.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
