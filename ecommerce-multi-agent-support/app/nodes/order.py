import re

from app.tools.order_tools import GetOrderStatusTool


ORDER_NO_PATTERN = re.compile(r"\bVLT-\d{4}-\d{4}\b", flags=re.IGNORECASE)


def extract_order_no(message: str) -> str | None:
    match = ORDER_NO_PATTERN.search(message)
    return match.group(0).upper() if match else None


class OrderStatusNode:
    def __init__(self, tool: GetOrderStatusTool):
        self.tool = tool

    def run(
        self,
        *,
        message: str,
        user_id: int,
        request_id: str,
    ) -> dict:
        order_no = extract_order_no(message)
        if order_no is None:
            return {
                "dispatched_to": "order",
                "order_id": None,
                "order_facts": None,
                "shipment_facts": None,
                "answer": "请提供格式为 VLT-YYYY-NNNN 的订单号，我才能查询订单和物流状态。",
                "tool_trace": [],
            }

        result = self.tool.execute(
            order_no=order_no,
            user_id=user_id,
            request_id=request_id,
        )
        if result.order is None:
            answer = "订单不存在或无权访问，请检查订单号是否属于当前账号。"
        else:
            answer = self._build_answer(result.order, result.shipment)
        return {
            "dispatched_to": "order",
            "order_id": order_no,
            "order_facts": result.order,
            "shipment_facts": result.shipment,
            "answer": answer,
            "tool_trace": [result.trace],
        }

    @staticmethod
    def _build_answer(order: dict, shipment: dict | None) -> str:
        item_summary = "、".join(
            f"{item['product_name']} x{item['quantity']}" for item in order["items"]
        )
        lines = [
            f"订单 {order['order_no']} 当前状态为 {order['status']}。",
            f"订单金额：¥{order['total_amount']}；商品：{item_summary}。",
        ]
        if shipment is None:
            lines.append("当前还没有可用的物流记录。")
        else:
            lines.append(
                f"物流状态：{shipment['status']}；承运商：{shipment['carrier']}；"
                f"运单号：{shipment['tracking_no']}。"
            )
            if shipment["exception_type"] != "none":
                lines.append(f"物流异常类型：{shipment['exception_type']}。")
            if shipment["estimated_delivery_at"] is not None:
                lines.append(f"预计送达时间：{shipment['estimated_delivery_at']}。")
        return "\n".join(lines)
