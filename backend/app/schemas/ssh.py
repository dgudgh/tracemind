from pydantic import BaseModel, Field


class SSHCommandRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=4000)
    timeout: int = Field(30, ge=1, le=600)
    confirm: bool = False


class SSHScriptDeployRequest(BaseModel):
    remote_path: str = Field(..., min_length=1, max_length=500)
    script_content: str = Field(..., min_length=1, max_length=200000)
    run: bool = False
    timeout: int = Field(60, ge=1, le=1800)
