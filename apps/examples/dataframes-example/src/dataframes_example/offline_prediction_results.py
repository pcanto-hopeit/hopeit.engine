"""Endpoint to retrieve datablock of predictions"""

from dataframes_example.iris import (
    IrisOfflinePredictionDataBlock,
    IrisOfflinePredictionDataBlockItem,
)

from hopeit.app.api import event_api
from hopeit.app.context import EventContext
from hopeit.dataframes import DataBlocks

from hopeit.dataframes.datablocks import TempDataBlock

__steps__ = ["load_datablock"]


__api__ = event_api(
    summary="Offline Prediction Results",
    payload=(IrisOfflinePredictionDataBlock, "Datablock generated by Predict Offline"),
    responses={200: list[IrisOfflinePredictionDataBlockItem]},
)


async def load_datablock(
    datablock: IrisOfflinePredictionDataBlock,
    context: EventContext,
) -> list[IrisOfflinePredictionDataBlockItem]:
    df = await DataBlocks.load(datablock)
    return TempDataBlock(IrisOfflinePredictionDataBlock, df).to_dataobjects(
        IrisOfflinePredictionDataBlockItem  # type: ignore[arg-type]
    )
