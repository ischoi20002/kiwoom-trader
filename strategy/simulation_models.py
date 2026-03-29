import numpy as np
import json
import os

class SimulatedRealtimeMetrics:
    """[v35.7] 자가 학습형 합성 모델 (Self-Learning Synthetic Model)
    실시간 시장 데이터를 관찰하여 체결강도 및 호가잔량 추정 파라미터를 스스로 튜닝함.
    """
    # 기본 계수 (학습 초기값)
    params = {
        "vp_multiplier": 5000.0,
        "vp_bias": 100.0,
        "ba_multiplier": 10.0,
        "ba_bias": 1.0,
        "learning_rate": 0.005,
        "total_calibrations": 0,
        "avg_error_vp": 0.0,
        "avg_error_ba": 0.0
    }
    
    PARAMS_FILE = "sim_model_params.json"
    _initialized = False

    @classmethod
    def load_params(cls):
        if os.path.exists(cls.PARAMS_FILE):
            try:
                with open(cls.PARAMS_FILE, 'r') as f:
                    cls.params.update(json.load(f))
                cls._initialized = True
            except: pass

    @classmethod
    def save_params(cls):
        try:
            with open(cls.PARAMS_FILE, 'w') as f:
                json.dump(cls.params, f, indent=4)
        except: pass

    @classmethod
    def calibrate(cls, real_vp, real_ba, price_change, vol_ratio):
        """[v35.7] 실시간 데이터를 기반으로 파라미터 미세 조정 (Online Learning)"""
        if not cls._initialized: cls.load_params()
        
        lr = cls.params["learning_rate"]
        
        # 1. VolPower Tuning (간이 경사하강법)
        # VP_pred = multiplier * (price_change) + bias (단순화된 모델)
        pred_vp = cls.simulate_vol_power(price_change, vol_ratio, add_noise=False)
        error_vp = real_vp - pred_vp
        
        # 가중치 업데이트 (부호와 크기에 비례하여 조정)
        if abs(price_change) > 0.0001:
            cls.params["vp_multiplier"] += lr * error_vp * np.sign(price_change)
        cls.params["vp_bias"] += lr * error_vp
        
        # 2. BidAsk Tuning
        pred_ba = cls.simulate_bid_ask_ratio(price_change, vol_ratio, add_noise=False)
        error_ba = real_ba - pred_ba
        
        if abs(price_change) > 0.0001:
            cls.params["ba_multiplier"] += lr * error_ba * np.sign(price_change)
        cls.params["ba_bias"] += lr * error_ba
        
        # 통계 갱신
        cls.params["total_calibrations"] += 1
        # 지수 이동 평균으로 오차 추적
        cls.params["avg_error_vp"] = cls.params["avg_error_vp"] * 0.99 + abs(error_vp) * 0.01
        cls.params["avg_error_ba"] = cls.params["avg_error_ba"] * 0.99 + abs(error_ba) * 0.01

    @classmethod
    def simulate_bid_ask_ratio(cls, price_change, vol_ratio, vol_power=None, add_noise=True):
        if not cls._initialized: cls.load_params()
        
        p = cls.params
        base_ratio = p["ba_bias"]
        
        # 1. 가격 및 계수 반영
        base_ratio += price_change * p["ba_multiplier"]
            
        # 2. 거래량 급증 보너스 (고정 로직 유지)
        if vol_ratio > 2.0:
            base_ratio += (0.3 if price_change > 0 else -0.3)
                
        # 3. 체결강도 정보가 있다면 연동
        if vol_power is not None:
            if vol_power > 150: base_ratio += 0.2
            elif vol_power < 70: base_ratio -= 0.2
                
        noise = np.random.normal(0, 0.05) if add_noise else 0
        return max(0.3, min(3.0, base_ratio + noise))

    @classmethod
    def simulate_bid_ask_ratio_vectorized(cls, price_changes, vol_ratios, vol_powers=None, add_noise=True):
        """[v35.8] Vectorized version of simulate_bid_ask_ratio for high-performance backtesting"""
        if not cls._initialized: cls.load_params()
        p = cls.params
        
        # 1. Base + Price Change
        base_ratios = p["ba_bias"] + price_changes * p["ba_multiplier"]
        
        # 2. Volume Surge Bonus
        base_ratios += np.where((vol_ratios > 2.0) & (price_changes > 0), 0.3, 
                               np.where((vol_ratios > 2.0) & (price_changes <= 0), -0.3, 0.0))
        
        # 3. VolPower Alignment
        if vol_powers is not None:
            base_ratios += np.where(vol_powers > 150, 0.2, 
                                   np.where(vol_powers < 70, -0.2, 0.0))
            
        if add_noise:
            base_ratios += np.random.normal(0, 0.05, size=len(price_changes))
            
        return np.clip(base_ratios, 0.3, 3.0)

    @classmethod
    def simulate_vol_power(cls, price_change, vol_ratio, add_noise=True):
        if not cls._initialized: cls.load_params()
        
        p = cls.params
        base_vp = p["vp_bias"]
        
        # 가격 변동성 반영
        base_vp += price_change * p["vp_multiplier"]
            
        # 거래량 보너스
        if vol_ratio > 1.5 and price_change > 0:
            base_vp += 20.0
            
        noise = np.random.normal(0, 5.0) if add_noise else 0
        return max(30.0, min(500.0, base_vp + noise))

    @classmethod
    def simulate_vol_power_vectorized(cls, price_changes, vol_ratios, add_noise=True):
        """[v35.8] Vectorized version of simulate_vol_power for high-performance backtesting"""
        if not cls._initialized: cls.load_params()
        p = cls.params
        
        # 1. Base + Price Change
        base_vps = p["vp_bias"] + price_changes * p["vp_multiplier"]
        
        # 2. Volume Bonus
        base_vps += np.where((vol_ratios > 1.5) & (price_changes > 0), 20.0, 0.0)
        
        if add_noise:
            base_vps += np.random.normal(0, 5.0, size=len(price_changes))
            
        return np.clip(base_vps, 30.0, 500.0)
