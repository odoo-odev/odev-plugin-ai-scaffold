"""Arguments of the ``analyze`` command."""

from odev.common import args


class Analyze:
    """Command line arguments shared by ``odev analyze`` and what extends it."""

    task_id = args.String(description="Id of the task to analyze")
    no_excalidraw = args.Flag(
        aliases=["-e", "--no-excalidraw"],
        description="Do not export the Excalidraw diagrams the task links to",
        default=False,
    )
    context = args.Flag(
        aliases=["-c", "--context"],
        description="Clone the client repository without asking: the analysis is made in the client's code, "
        "which is otherwise offered and can be declined",
        default=False,
    )
    llm = args.String(
        aliases=["--llm"],
        description="Name of the LLM to use",
        default="",
    )
    odoo_version = args.String(
        aliases=["--odoo-version"],
        description="Odoo version to analyze for, e.g. 17.0. Read from the database or the task, "
        "or asked for, when omitted",
        default="",
    )
    # Not named "platform": DatabaseCommand already owns -p/--platform, where it
    # picks the platform odev looks the database up on, not the one it is hosted on.
    hosting = args.String(
        aliases=["--hosting"],
        description="Hosting of the client database: saas, sh or op. Read from the database or the task, "
        "or asked for, when omitted",
        default="",
    )
    client = args.String(
        aliases=["--client"],
        description="Name of the client. Read from the database or the task, or asked for, when omitted",
        default="",
    )
    comment = args.String(
        aliases=["--comment"],
        description="Instructions for this run, read before anything else: they take precedence over the "
        "task description and the method wherever they disagree",
        default="",
    )
