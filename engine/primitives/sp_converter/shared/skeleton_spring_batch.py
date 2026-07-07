"""M4 Stage C — the ``spring-batch`` skeleton renderer.

Emits a small Spring Batch starter tree for the SP: a ``pom.xml``, a ``<Sp>Job``
job-config class, and a ``<Sp>Step`` chunk-step stub. The renderer is forbidden
from inventing business logic — every generated method body carries an explicit
``// TODO(<sp>): ...`` marker for the engineer to complete, and the gotchas /
data-deps come straight from the (anti-hallucination-guarded) plan.

Deterministic: the same :class:`ConversionPlan` always renders the same tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.sp_converter.shared.models import ConversionArtifact
from primitives.sp_converter.shared.skeletons import register

if TYPE_CHECKING:
    from primitives.sp_converter.shared.models import ConversionPlan

_JAVA_PKG = "com.example.conversion"
_JAVA_DIR = "src/main/java/com/example/conversion"


def _class_base(sp_name: str) -> str:
    """PascalCase a stored-proc name into a Java class basename."""
    parts = [p for p in sp_name.replace("-", "_").split("_") if p]
    if not parts:
        return "Sp"
    return "".join(p[:1].upper() + p[1:].lower() for p in parts)


def render(plan: ConversionPlan) -> list[ConversionArtifact]:
    """Render the ``spring-batch`` starter tree for *plan*."""
    base = _class_base(plan.sp_name)
    deps = ", ".join(plan.data_deps) or "(none resolved)"
    gotchas = "\n".join(f" *   - {g}" for g in plan.gotchas) or " *   - (none recorded)"

    pom = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        f"  <groupId>{_JAVA_PKG}</groupId>\n"
        f"  <artifactId>{plan.sp_name.lower()}-batch</artifactId>\n"
        "  <version>0.1.0-SNAPSHOT</version>\n"
        "  <dependencies>\n"
        "    <dependency>\n"
        "      <groupId>org.springframework.boot</groupId>\n"
        "      <artifactId>spring-boot-starter-batch</artifactId>\n"
        "    </dependency>\n"
        "  </dependencies>\n"
        "</project>\n"
    )

    job = (
        f"package {_JAVA_PKG};\n\n"
        "import org.springframework.batch.core.Job;\n"
        "import org.springframework.batch.core.Step;\n"
        "import org.springframework.batch.core.job.builder.JobBuilder;\n"
        "import org.springframework.context.annotation.Bean;\n"
        "import org.springframework.context.annotation.Configuration;\n\n"
        "/**\n"
        f" * Spring Batch job converted from stored procedure {plan.sp_name}.\n"
        f" * Data dependencies: {deps}\n"
        " * Gotchas:\n"
        f"{gotchas}\n"
        " */\n"
        "@Configuration\n"
        f"public class {base}Job {{\n"
        "  @Bean\n"
        f"  public Job {base.lower()}Job(/* JobRepository, Step */) {{\n"
        f"    // TODO({plan.sp_name}): wire the JobBuilder with the {base}Step below.\n"
        '    throw new UnsupportedOperationException("generated scaffold — fill in");\n'
        "  }\n"
        "}\n"
    )

    step = (
        f"package {_JAVA_PKG};\n\n"
        "import org.springframework.batch.core.Step;\n"
        "import org.springframework.context.annotation.Bean;\n"
        "import org.springframework.context.annotation.Configuration;\n\n"
        "/**\n"
        f" * Chunk step for the {plan.sp_name} conversion.\n"
        " */\n"
        "@Configuration\n"
        f"public class {base}Step {{\n"
        "  @Bean\n"
        f"  public Step {base.lower()}Step(/* StepBuilder, reader, processor, writer */) {{\n"
        f"    // TODO({plan.sp_name}): translate the cursor/loop body into a chunk reader+writer.\n"
        '    throw new UnsupportedOperationException("generated scaffold — fill in");\n'
        "  }\n"
        "}\n"
    )

    return [
        ConversionArtifact(path="pom.xml", content=pom, kind="config"),
        ConversionArtifact(path=f"{_JAVA_DIR}/{base}Job.java", content=job, kind="scaffold"),
        ConversionArtifact(path=f"{_JAVA_DIR}/{base}Step.java", content=step, kind="scaffold"),
    ]


register("spring-batch", render)

__all__ = ["render"]
