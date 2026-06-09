import click
from rich.console import Console

console = Console()

from .database import init_db
from .commands import import_cmd, list_cmd, calc_cmd, notice_cmd, send_cmd, record_cmd, report_cmd


@click.group(help="物业费催缴命令行工具 - 批量处理欠费提醒")
@click.version_option("1.0.0", prog_name="pfee")
def cli():
    init_db()
    pass


cli.add_command(import_cmd.import_cmd)
cli.add_command(list_cmd.list_cmd)
cli.add_command(calc_cmd.calc_cmd)
cli.add_command(notice_cmd.notice_cmd)
cli.add_command(send_cmd.send_cmd)
cli.add_command(record_cmd.record_cmd)
cli.add_command(report_cmd.report_cmd)


if __name__ == "__main__":
    cli()
