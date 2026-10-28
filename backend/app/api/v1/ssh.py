from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.ssh import SSHCommandRequest, SSHScriptDeployRequest
from app.services import instances as instance_service
from app.services import ssh_executor as ssh_service

router = APIRouter(prefix="/ssh", tags=["SSH 远程运维"])


def _get_instance(db: Session, instance_id: int):
    instance = instance_service.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="数据库实例不存在")
    return instance


@router.post("/{instance_id}/test")
def ssh_test(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_instance(db, instance_id)
    return ssh_service.test_ssh_connection(instance)


@router.post("/{instance_id}/diagnose")
def ssh_diagnose(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_instance(db, instance_id)
    return ssh_service.diagnose_ssh_connection(instance)


@router.get("/{instance_id}/system-info")
def ssh_system_info(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_instance(db, instance_id)
    return ssh_service.get_system_info(instance)


@router.post("/{instance_id}/execute")
def ssh_execute(instance_id: int, payload: SSHCommandRequest, db: Session = Depends(get_db)):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="执行远程命令需要 confirm=true")
    instance = _get_instance(db, instance_id)
    return ssh_service.execute_command(instance, payload.command, payload.timeout)


@router.post("/{instance_id}/deploy-script")
def ssh_deploy_script(instance_id: int, payload: SSHScriptDeployRequest, db: Session = Depends(get_db)):
    instance = _get_instance(db, instance_id)
    return ssh_service.deploy_script(
        instance,
        payload.script_content,
        payload.remote_path,
        payload.run,
        payload.timeout,
    )
