from villani_ops.git.generated import is_generated_or_cache_path, split_generated_paths


def test_generated_filter_keeps_source_and_filters_cache():
    paths=["src/signalshop/pricing.py","src/signalshop/__pycache__/pricing.cpython-311.pyc",".pytest_cache/v/cache/nodeids"]
    kept, generated=split_generated_paths(paths)
    assert kept == ["src/signalshop/pricing.py"]
    assert "src/signalshop/__pycache__/pricing.cpython-311.pyc" in generated
    assert ".pytest_cache/v/cache/nodeids" in generated
    assert is_generated_or_cache_path("build/foo.o")
