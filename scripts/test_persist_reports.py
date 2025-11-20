import asyncio
import os
from core.execution import ExecutionManager
from core.risk_management import RiskManager, Position

async def main():
    # Crear ExecutionManager en modo simulación (private_client=None)
    rm = RiskManager()
    em = ExecutionManager(private_client=None, risk_manager=rm)

    # Crear una posición de ejemplo
    pos = Position(
        symbol='ETH/USDT'.replace('/','/'),
        direction='LONG',
        size_base=0.1234,
        size_usd=200.0,
        entry_price=1600.0,
        entry_time='2025-11-16T12:00:00',
        entry_idx=0,
        stop_loss=1500.0,
        original_stop_loss=1500.0,
        take_profit=1800.0,
        liquidation_price=0.0,
        margin_used=13.33,
        is_copy=False,
        setup_type='TEST'
    )

    # Añadir a open_positions y persistir report
    em.open_positions['ETH/USDT'] = pos
    await em._persist_live_reports()

    # Mostrar archivos en reports/
    base_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    reports_dir = os.path.join(base_dir, 'reports')
    print('Reports dir:', reports_dir)
    for f in os.listdir(reports_dir):
        if f.startswith('live_report_SMC_'):
            path = os.path.join(reports_dir, f)
            print(f"- {f} -> {os.path.getsize(path)} bytes")

if __name__ == '__main__':
    asyncio.run(main())
