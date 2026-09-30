from truhowl.maintenance_agents import analyze_impact


def test_impact_analyst_dummy(tmp_path):
    res = analyze_impact(str(tmp_path), "stripe")
    assert res.provider == "stripe"
    assert isinstance(res.affected_files, list)
