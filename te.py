import MetaTrader5 as mt5




mt5.initialize()
ti = mt5.terminal_info()
ai = mt5.account_info()
print("connected:", ti.connected, "tradeAllowed:", ti.trade_allowed)
print("login:", ai.login if ai else None)
print("last_error:", mt5.last_error())