from .dayz_monitor import DayZMonitor


async def setup(bot):
    await bot.add_cog(DayZMonitor(bot))
