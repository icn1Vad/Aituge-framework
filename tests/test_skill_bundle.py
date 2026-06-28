import asyncio

from skill import (
    SkillBundle,
    build_skill_bundle,
    create_read_skill_tool,
    list_skills,
    load_skill,
)


def test_load_skill_and_render_bundle():
    primary = load_skill("task-style")
    bundle = SkillBundle.from_skills(primary=primary)

    rendered = bundle.render_prompt()

    assert primary.name == "task-style"
    assert primary.description == "Keep task answers concise and include the active skill marker."
    assert "# Primary Task Skill" in rendered
    assert "skill-active: task-style" in rendered


def test_list_skills_and_build_bundle_from_names():
    summaries = list_skills()
    names = {summary.name for summary in summaries}

    assert {
        "task-style",
        "review-style",
        "implementation-plan",
        "debugging-checklist",
        "concise-summary",
        "report-generator",
        "report-executive-summary",
        "report-context-scope",
        "report-analysis-findings",
        "report-risk-actions",
        "report-quantitative-calculation",
        "report-chart-figure",
        "report-code-verification",
    }.issubset(names)

    bundle = build_skill_bundle(
        primary_skill="task-style",
        candidate_skills=["review-style"],
    )
    rendered = bundle.render_prompt()

    assert bundle.primary.name == "task-style"
    assert [skill.name for skill in bundle.candidates] == ["review-style"]
    assert "# Available Auxiliary Skills" in rendered
    assert "review-style: Add a brief risk check" in rendered


def test_build_different_primary_skill_types():
    bundle = build_skill_bundle(
        primary_skill="debugging-checklist",
        candidate_skills=["implementation-plan", "concise-summary"],
    )
    rendered = bundle.render_prompt()

    assert "skill-active: debugging-checklist" in rendered
    assert "implementation-plan: Structure coding tasks" in rendered
    assert "concise-summary: Summarize results" in rendered


def test_build_report_generator_skill_package():
    bundle = build_skill_bundle(
        primary_skill="report-generator",
        candidate_skills=[
            "report-executive-summary",
            "report-context-scope",
            "report-analysis-findings",
            "report-quantitative-calculation",
            "report-chart-figure",
            "report-code-verification",
            "report-risk-actions",
        ],
    )
    rendered = bundle.render_prompt()

    assert bundle.primary.name == "report-generator"
    assert "skill-active: report-generator" in rendered
    assert "The report is not complete until every required section has been written." in rendered
    assert "each section must be written from its matching" in rendered
    assert "call ReadSkill with skill_name before applying" in rendered
    assert "do not call multiple ReadSkill tools in parallel" in rendered
    assert "Do not write a section from this primary skill alone" in rendered
    assert "report-executive-summary: Write the report opening" in rendered
    assert "report-quantitative-calculation: Use code to calculate report metrics" in rendered
    assert "report-chart-figure: Use code to create report charts" in rendered
    assert "report-risk-actions: Close a report with risks" in rendered


def test_create_read_skill_tool_reads_auxiliary_skill():
    async def run():
        bundle = build_skill_bundle(
            primary_skill="report-generator",
            candidate_skills=["report-analysis-findings"],
        )
        tool = create_read_skill_tool(bundle)

        assert tool is not None
        assert tool.metadata.name == "ReadSkill"
        output = await tool.acall(skill_name="report-analysis-findings")

        assert "# Skill: report-analysis-findings" in output.content
        assert "Use this auxiliary skill for the Analysis and Findings section" in output.content

    asyncio.run(run())


def test_read_skill_tool_reads_code_oriented_report_skill():
    async def run():
        bundle = build_skill_bundle(
            primary_skill="report-generator",
            candidate_skills=["report-chart-figure"],
        )
        tool = create_read_skill_tool(bundle)

        output = await tool.acall(skill_name="report-chart-figure")

        assert "LimitedLocalPythonInterpreter" in output.content
        assert "Save the figure as `.png` or `.svg`" in output.content

    asyncio.run(run())
