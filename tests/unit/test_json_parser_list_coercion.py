from orchestra.prompts.parsers import parse_output
from orchestra.schemas.artifacts import AlgorithmPlanArtifact


def test_json_parser_coerces_string_list_fields():
    text = """
    {
      "problem_summary": "Echo two integers.",
      "algorithm": "Read A and B, print Yes if adjacent.",
      "data_structures": "None required beyond storing two integers.",
      "correctness_argument": "Direct check.",
      "time_complexity": "O(1)",
      "space_complexity": "O(1)",
      "edge_cases": "Cases where A is 3 or 6.\\nValues in 1..9.",
      "implementation_notes": "Read two integers from input."
    }
    """
    parsed = parse_output("json", text, "AlgorithmPlanArtifact", "algorithm_analyst")
    assert isinstance(parsed, AlgorithmPlanArtifact)
    assert parsed.data_structures == [
        "None required beyond storing two integers."
    ]
    assert parsed.edge_cases == [
        "Cases where A is 3 or 6.",
        "Values in 1..9.",
    ]
    assert parsed.implementation_notes == ["Read two integers from input."]
