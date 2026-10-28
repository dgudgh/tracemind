from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ZabbixConfig(Base):
    __tablename__ = "zabbix_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    password_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_token_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ZabbixHostMapping(Base):
    __tablename__ = "zabbix_host_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zabbix_config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    zabbix_hostid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    zabbix_host_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    visible_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(100), nullable=True)
    db_instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ZabbixProblemCache(Base):
    __tablename__ = "zabbix_problem_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zabbix_config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    eventid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    objectid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hostid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    host_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    problem_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    severity_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    clock: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="firing", index=True)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sync_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ZabbixPushConfig(Base):
    __tablename__ = "zabbix_push_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_name: Mapped[str] = mapped_column(String(100), nullable=False)
    zabbix_server: Mapped[str] = mapped_column(String(200), nullable=False)
    zabbix_port: Mapped[int] = mapped_column(Integer, default=10051)
    zabbix_host: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ZabbixPushLog(Base):
    __tablename__ = "zabbix_push_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    alert_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    alert_level: Mapped[str | None] = mapped_column(String(50), nullable=True)
    zabbix_host: Mapped[str | None] = mapped_column(String(200), nullable=True)
    zabbix_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    push_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    push_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class PrometheusConfig(Base):
    __tablename__ = "prometheus_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prometheus_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    alertmanager_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class PrometheusAlertCache(Base):
    __tablename__ = "prometheus_alert_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    alert_status: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    severity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    instance_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    db_instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    labels_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    annotations_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
