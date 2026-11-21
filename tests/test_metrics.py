import pytest

from evaluation.metrics import portfolio_return, maximum_drawdown

@pytest.mark.parametrize(
    "values, expected_return, expected_drawdown",
    [
        ([100, 120, 90, 110], 0.10, 0.25),
        ([100, 110, 120], 0.20, 0.0),
        ([100, 80, 60], -0.40, 0.40),
        ([100, 100, 100], 0.0, 0.0),
        ([100], 0.0, 0.0),
        ([100, 80, 120, 90], -0.10, 0.25),
    ],
)
def test_portfolio_metrics(values, expected_return, expected_drawdown):
    assert portfolio_return(values) == pytest.approx(expected_return)
    assert maximum_drawdown(values) == pytest.approx(expected_drawdown)

@pytest.mark.parametrize(
    "values",
    [
        [],
        [0,100],
        [100, -10],
        [100, float("nan")],
        [100, float("inf")],
        [100, True],
        [100, "110"],
    ],
)
def test_invalid_portfolio_values(values):
    with pytest.raises(ValueError):
        portfolio_return(values)

    with pytest.raises(ValueError):
        maximum_drawdown(values)
