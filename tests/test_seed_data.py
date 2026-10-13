from concurrent.futures import ThreadPoolExecutor

from app.db.seed_data import builtin_filter_groups


def test_builtin_filter_groups_are_stable_for_repeated_and_concurrent_reads():
    baseline = builtin_filter_groups()
    groups = {group["id"]: group for group in baseline}

    assert {1000, 1001, 9999}.issubset(groups)
    assert groups[9999]["rules"] == []
    assert all(isinstance(group["id"], int) for group in baseline)
    assert groups[1000]["rules"] and groups[1001]["rules"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        concurrent_results = list(executor.map(lambda _: builtin_filter_groups(), range(4)))

    assert builtin_filter_groups() == baseline
    assert all(result == baseline for result in concurrent_results)
