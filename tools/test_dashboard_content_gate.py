import dashboard_content_gate as gate


POLICY = {
    'private_repositories': ['honua-esri-compat'],
    'restricted_terms': ['licensed desktop product'],
}


def page(row):
    return f'<html><head><style>.hidden {{ color: red }}</style></head><body><table>{row}</table></body></html>'


def test_clean_page_passes():
    assert gate.violations(page('<tr><td>honua-server#12</td><td>Fix public endpoint</td></tr>'), POLICY) == []


def test_private_repository_title_fails_and_names_reference():
    problems = gate.violations(
        page('<tr><td>honua-esri-compat#74</td><td>Secret certification plan</td></tr>'), POLICY)
    assert any('honua-esri-compat#74' in problem and 'Secret certification plan' in problem for problem in problems)


def test_restricted_term_fails():
    problems = gate.violations(page('<tr><td>Detail for licensed desktop product</td></tr>'), POLICY)
    assert any("restricted term 'licensed desktop product'" in problem for problem in problems)


def test_neutral_private_reference_row_passes():
    reference = 'honua-esri-compat#74'
    assert gate.violations(page(f'<tr><td>{reference}</td><td>Evidence {reference}</td></tr>'), POLICY) == []
