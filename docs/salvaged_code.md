# Salvaged Code Snippets from Get_Price_GUI.py

These snippets were captured from the conversation history before the file was corrupted. Use these to manually restore the core logic and UI changes.

### 1. Updated DEFAULT_SETTINGS
```python
DEFAULT_SETTINGS = {
    "rsi_min": 40.0,
    "rsi_max": 65.0,
    "rsi_max_type_a": 58.0,
    "min_trde_val": 40.0,
    "vol_ratio": 1.5,
    "vol_surge": 1.2,
    "vol_lookback": 10,
    "ma_convergence": 0.03,
    "use_type_a": True,
    "use_type_b": True,
    # ... other settings as before
}
```

### 2. Updated Strategy Tab (Group A & B)
```python
        # Section A
        add_signal_row(group_a, "use_gc", "1. 골든크로스 15분", label_text="(이평선 간격 조건 포함)")
        add_signal_row(group_a, "use_type_a", "2. RSI 눌림 상한", "rsi_max_type_a", "", "RSI 임계값:") 
        add_signal_row(group_a, "use_ma60_safe", "5. MA60 안전구역", "ma60_min", "%", "주분 위치:", "ma60_max", "%")

        # Section B
        add_signal_row(group_b, "use_type_b", "RSI 돌파 상한", "rsi_max", "", "RSI 임계값:")
        add_signal_row(group_b, "use_vol_power", "3. 체결강도 강화", "vol_power_lvl", "", "기준치")
        add_signal_row(group_b, "use_vol_spike", "4. 거래량 순간급증", "vol_spike_ratio", "배", "분봉 정규분포")
```

### 3. Updated Summary Label
```python
    def _get_criteria_text(self, format_type="ui"):
        s = self.settings
        b = "▶ " if format_type == "ui" else "■ "
        line_break = "\n" if format_type == "ui" else "\n"
        return (
            f"{b}KOSPI 200 종목  |  {b}5일평균거래금 {s.get('min_trde_val', 40)}억  |  {b}MA5-20 수렴 {int(s.get('ma_convergence', 0.03)*100)}% 이내{line_break}"
            f"{b}최근 {int(s.get('vol_lookback', 10))}일간 거래량 {int(s.get('vol_ratio', 1.5)*100)}%+ 분출  |  {b}RSI(14) {s.get('rsi_min', 40)} ~ [A:{s.get('rsi_max_type_a', 58)} / B:{s.get('rsi_max', 65)}]"
        )
```

### 4. Real-time Entry Signal Logic (Core)
```python
                                            # RSI Signal Logic
                                            if last_rsi_f > 0 and last_rsi_f < 50.0 and rsi_val >= 50.0:
                                                s_state_raw['signal_time'] = now_dt
                                                self.log(f"🔔 [SIGNAL ON] {info['name']}({code}) RSI 50 상향 돌파! (Now: {rsi_val:.1f})")
                                            
                                            if s_state_raw['signal_time']:
                                                elapsed_sec = (now_dt - s_state_raw['signal_time']).total_seconds()
                                                if rsi_val < 50.0:
                                                    s_state_raw['signal_time'] = None
                                                elif elapsed_sec > self.settings.get('rsi_window', 45) * 60:
                                                    s_state_raw['signal_time'] = None
```
