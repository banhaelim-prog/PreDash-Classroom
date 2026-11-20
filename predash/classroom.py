"""Broker connection settings held only in the current Streamlit session."""
import streamlit as st
from predash.kis import KIS, BrokerError
from predash.toss import Toss


def account_settings(mode=None):
    values = st.session_state.get("classroom_credentials", {})
    provider = values.get("provider", "kis")
    if provider == "toss":
        return {
            "provider": "toss", "mode": "real",
            "key": values.get("client_id", ""),
            "secret": values.get("client_secret", ""),
            "cano": str(values.get("account_seq", "")),
            "product": "TS",
            "account": str(values.get("account_seq", "")),
        }
    selected = mode or values.get("mode", "demo")
    if selected != values.get("mode"):
        return dict(provider="kis", mode=selected, key="", secret="", cano="", product="")
    return dict(provider="kis", mode=selected,
                **{k: values.get(k, "") for k in ("key", "secret", "cano", "product")})


def connection_form():
    if st.session_state.pop("classroom_clear_inputs", False):
        for key in ("class_key", "class_secret", "class_cano", "class_product",
                    "toss_client_id", "toss_client_secret", "toss_account_seq"):
            st.session_state.pop(key, None)
    st.subheader("내 증권사 계좌 연결")
    st.caption("증권사를 선택하고 본인 인증정보를 입력하세요. 입력값은 현재 접속 세션에서만 사용합니다.")
    existing = st.session_state.get("classroom_credentials")
    if existing:
        provider = existing.get("provider", "kis")
        label = "토스증권 Open API" if provider == "toss" else "한국투자증권"
        st.success(f"{label} 연결됨 · " + ("실전 계좌 조회" if provider == "toss" or existing.get("mode") == "real" else "모의투자"))
        if st.button("계좌 연결 해제"):
            authorized = st.session_state.get("authorized")
            st.session_state.clear()
            if authorized:
                st.session_state.authorized = True
            st.rerun()
        return

    provider_label = st.radio("증권사", ["토스증권", "한국투자증권"], horizontal=True)
    if provider_label == "토스증권":
        st.caption("토스증권 WTS > 설정 > Open API에서 발급한 Client ID·Secret과 계좌 목록의 accountSeq를 입력하세요.")
        with st.form("toss_connection"):
            client_id = st.text_input("Client ID", type="password", key="toss_client_id")
            client_secret = st.text_input("Client Secret", type="password", key="toss_client_secret")
            account_seq = st.text_input("accountSeq", key="toss_account_seq")
            submitted = st.form_submit_button("토스증권 연결 확인", type="primary", use_container_width=True)
        if submitted:
            settings = {"provider": "toss", "mode": "real", "key": client_id.strip(),
                        "secret": client_secret.strip(), "account": account_seq.strip()}
            try:
                client = Toss(settings=settings)
                with st.spinner("토스증권 토큰과 계좌 잔고 조회 권한을 확인합니다…"):
                    client.balance()
                st.session_state.classroom_credentials = {
                    "provider": "toss", "client_id": client_id.strip(),
                    "client_secret": client_secret.strip(), "account_seq": account_seq.strip(),
                    "mode": "real",
                }
                st.session_state["_toss_client"] = client
                st.session_state.classroom_clear_inputs = True
                st.rerun()
            except BrokerError as error:
                st.error(str(error))
        st.info("토스증권 WTS의 Open API 허용 IP 관리에 Streamlit 서버의 외부 IP를 등록해야 합니다. API 키는 GitHub에 올리지 마세요.")
        st.link_button("토스증권 Open API 발급·안내", "https://home.tossinvest.com/ko/open-api")
        st.link_button("토스증권 개발자 문서", "https://developers.tossinvest.com/docs")
        return

    with st.form("classroom_connection"):
        mode = st.radio("투자 환경", ["모의투자", "실전 조회"], horizontal=True)
        key = st.text_input("App Key", type="password", key="class_key")
        secret = st.text_input("App Secret", type="password", key="class_secret")
        cano = st.text_input("계좌번호 앞 8자리", type="password", max_chars=8, key="class_cano")
        product = st.text_input("계좌번호 뒤 2자리", max_chars=2, key="class_product")
        submitted = st.form_submit_button("한국투자증권 연결 확인", type="primary", use_container_width=True)
    if submitted:
        settings = dict(provider="kis", mode="demo" if mode == "모의투자" else "real",
                        key=key.strip(), secret=secret.strip(), cano=cano.strip(), product=product.strip())
        try:
            client = KIS(settings=settings)
            with st.spinner("한국투자증권 잔고 조회 권한을 확인합니다…"):
                client.balance()
            st.session_state.classroom_credentials = settings
            st.session_state["_kis_client_demo" if settings["mode"] == "demo" else "_kis_client"] = client
            st.session_state.classroom_clear_inputs = True
            st.rerun()
        except BrokerError as error:
            st.error(str(error))
    st.info("연결 해제와 로그아웃은 키·잔고·접속 중 실습 자료를 지웁니다. 필요한 기록은 먼저 백업하세요.")
