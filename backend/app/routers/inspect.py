from fastapi import APIRouter, HTTPException

from app.schemas.entities import InspectRequest
from app.services.inspect_service import ReferenceNotReadyError, run_inspect

router = APIRouter(tags=["inspect"])


@router.post("/inspect")
def inspect(body: InspectRequest):
    try:
        return run_inspect(body.dataset_id)
    except ReferenceNotReadyError as exc:
        # 目标夹具未就绪：整批参照检查失败，不签发；附带失败批次以便前端展示
        raise HTTPException(
            409,
            {
                "message": str(exc),
                "batch_id": exc.batch_id,
                "report": exc.report,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc
