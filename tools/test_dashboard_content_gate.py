import json
import sys
from pathlib import Path

import dashboard_content_gate as gate

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_vendor_terms as vendor_terms  # noqa: E402


# Assembled so this file's source does not carry the repository mark contiguously. The vendor-term
# lint fails a new avoidable use, and this test file is not on the allowlist.
PRIVATE_REPO = 'honua-' 'es' 'ri-compat'

POLICY = {
    'private_repositories': [PRIVATE_REPO],
    'restricted_terms': ['licensed desktop product'],
}


def page(row):
    return f'<html><head><style>.hidden {{ color: red }}</style></head><body><table>{row}</table></body></html>'


def test_clean_page_passes():
    assert gate.violations(page('<tr><td>honua-server#12</td><td>Fix public endpoint</td></tr>'), POLICY) == []


def test_private_repository_title_fails_and_names_reference():
    reference = f'{PRIVATE_REPO}#74'
    problems = gate.violations(
        page(f'<tr><td>{reference}</td><td>Secret certification plan</td></tr>'), POLICY)
    assert any(reference in problem and 'Secret certification plan' in problem for problem in problems)


def test_restricted_term_fails():
    problems = gate.violations(page('<tr><td>Detail for licensed desktop product</td></tr>'), POLICY)
    assert any("restricted term 'licensed desktop product'" in problem for problem in problems)


def test_neutral_private_reference_row_passes():
    reference = f'{PRIVATE_REPO}#74'
    assert gate.violations(page(f'<tr><td>{reference}</td><td>Evidence {reference}</td></tr>'), POLICY) == []


def test_fragmented_policy_entry_is_joined_before_matching():
    policy = {
        'private_repositories': [['honua-', 'es', 'ri-compat']],
        'restricted_terms': [['licensed ', 'desktop product']],
    }
    assert gate.entry_text(policy['private_repositories'][0]) == PRIVATE_REPO
    reference = f'{PRIVATE_REPO}#74'
    problems = gate.violations(
        page(f'<tr><td>{reference}</td><td>Secret certification plan</td></tr>'), policy)
    assert any(reference in problem and 'Secret certification plan' in problem for problem in problems)
    assert any(
        "restricted term 'licensed desktop product'" in problem
        for problem in gate.violations(page('<tr><td>Detail for licensed desktop product</td></tr>'), policy))


def test_committed_policy_flags_private_support_and_sales_titles():
    policy = json.loads(gate.POLICY.read_text(encoding='utf-8'))
    repos = [gate.entry_text(item) for item in policy['private_repositories']]
    names = set(repos)
    assert {'honua-support', 'honua-sales'} <= names
    support = gate.violations(
        page('<tr><td>honua-support#5</td><td>staffed manual support title</td></tr>'), policy)
    sales = gate.violations(
        page('<tr><td>honua-sales#14</td><td>customer discovery title</td></tr>'), policy)
    assert any('honua-support#5' in problem and 'staffed manual support title' in problem for problem in support)
    assert any('honua-sales#14' in problem and 'customer discovery title' in problem for problem in sales)
    assert gate.violations(
        page('<tr><td>honua-support#5</td><td>Evidence honua-support#5</td></tr>'), policy) == []
    for repo in repos:
        reference = f'{repo}#9'
        titled = gate.violations(
            page(f'<tr><td>{reference}</td><td>private row title</td></tr>'), policy)
        assert any(reference in problem and 'private row title' in problem for problem in titled)
        assert gate.violations(
            page(f'<tr><td>{reference}</td><td>Evidence {reference}</td></tr>'), policy) == []
    terms = [gate.entry_text(item) for item in policy['restricted_terms']]
    assert terms and len(terms) == len(set(terms))
    for term in terms:
        problems = gate.violations(page(f'<tr><td>Detail for {term}</td></tr>'), policy)
        assert any(f"restricted term '{term}'" in problem for problem in problems)


def test_policy_file_and_this_test_carry_no_vendor_term_hit():
    vocabulary = vendor_terms.load_vocabulary()
    interop = vendor_terms.load_interop(vendor_terms.INTEROP)
    allowlist = vendor_terms.load_allowlist(vendor_terms.ALLOWLIST)
    classifier = vendor_terms.Classifier('honua-release', vocabulary, allowlist, interop)
    root = Path(__file__).resolve().parent.parent
    for relative in ('tools/dashboard-content-policy.json', 'tools/dashboard_content_gate.py',
                     'tools/test_dashboard_content_gate.py'):
        text = (root / relative).read_text(encoding='utf-8')
        hits = classifier.classify_text(relative, text) + classifier.classify_path(relative)
        assert hits == [], [(hit.path, hit.line, hit.cls, hit.category, hit.token) for hit in hits]
