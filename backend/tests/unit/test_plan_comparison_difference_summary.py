from src.services.plan_comparison_preview_service import PlanComparisonPreviewService


def test_difference_summary_uses_required_and_flexible_lineage():
    days = [
        {
            "segments": [
                {
                    "title": "清华大学",
                    "poi": {"name": "清华大学"},
                    "semanticMetadata": {"requirementLevel": "required"},
                },
                {
                    "title": "模式口历史文化街区",
                    "poi": {"name": "模式口历史文化街区"},
                    "semanticMetadata": {"optionalExperienceFamily": "heritage_walk"},
                },
            ]
        }
    ]

    summary = PlanComparisonPreviewService._difference_summary(days, [], {})

    assert "共享必选：清华大学" in summary
    assert "本方案新增：模式口历史文化街区" in summary
    assert "侧重：heritage_walk" in summary
