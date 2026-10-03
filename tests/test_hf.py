"""Tests for Hugging Face repo detection. Pure and synchronous: no network."""

import unittest

from locker.hf import detect_repos, is_repo_id


class DetectionTests(unittest.TestCase):
    def test_detects_hf_flag(self):
        self.assertEqual(
            detect_repos("llama-server -hf Qwen/Qwen3-8B"), ["Qwen/Qwen3-8B"]
        )

    def test_detects_hf_repo_and_repo_id_flags(self):
        for flag in ("--hf", "--hf-repo", "--repo-id"):
            with self.subTest(flag=flag):
                self.assertEqual(
                    detect_repos(f"tool {flag} Qwen/Qwen3-8B"), ["Qwen/Qwen3-8B"]
                )

    def test_detects_model_flag_value(self):
        self.assertEqual(
            detect_repos("llama-server --model unsloth/Qwen3-8B-GGUF"),
            ["unsloth/Qwen3-8B-GGUF"],
        )

    def test_model_flag_with_local_path_is_skipped(self):
        self.assertEqual(detect_repos("llama-server --model ./models/qwen.gguf"), [])

    def test_model_flag_with_absolute_path_is_skipped(self):
        self.assertEqual(detect_repos("llama-server --model /models/x.gguf"), [])

    def test_gguf_extension_is_skipped(self):
        self.assertEqual(
            detect_repos("llama-server -hf Qwen/Qwen3-8B-GGUF/./x.gguf"), []
        )

        for suffix in (".gguf", ".safetensors", ".bin", ".pt"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    detect_repos(f"llama-server -hf owner/name{suffix}"), []
                )

    def test_url_is_skipped(self):
        found = detect_repos("open https://huggingface.co/Qwen/Qwen3-8B")

        self.assertEqual([c for c in found if "://" in c], [])

    def test_bare_token_is_detected(self):
        self.assertEqual(
            detect_repos("some-tool Qwen/Qwen3-8B --port 8080"), ["Qwen/Qwen3-8B"]
        )

    def test_home_relative_path_is_skipped(self):
        self.assertEqual(detect_repos("llama-server -m ~/models/qwen"), [])

    def test_three_segment_path_is_skipped(self):
        self.assertEqual(detect_repos("tool a/b/c"), [])

    def test_flags_come_before_bare_tokens(self):
        self.assertEqual(
            detect_repos("tool --model X/Y Z/W"), ["X/Y", "Z/W"]
        )

    def test_duplicates_are_collapsed(self):
        self.assertEqual(
            detect_repos("tool -hf Qwen/Qwen3-8B Qwen/Qwen3-8B"), ["Qwen/Qwen3-8B"]
        )

    def test_quant_suffix_is_stripped(self):
        # llama.cpp documents `-hf <repo>[:quant]`; the quant names a file inside
        # the repo, so the repo is everything before the colon.
        self.assertEqual(
            detect_repos("llama-server -hf unsloth/Qwen3-8B-GGUF:Q4_K_M"),
            ["unsloth/Qwen3-8B-GGUF"],
        )

    def test_no_candidates_returns_empty_list(self):
        self.assertEqual(detect_repos("llama-server --port 8080"), [])

    def test_is_repo_id_rejects_bad_shapes(self):
        for bad in ("", "a", "a/", "/b", "a/b/c", "a b/c"):
            with self.subTest(candidate=bad):
                self.assertFalse(is_repo_id(bad))

        self.assertTrue(is_repo_id("Qwen/Qwen3-8B"))
        self.assertTrue(is_repo_id("unsloth/Qwen3-8B-GGUF"))


if __name__ == "__main__":
    unittest.main()
