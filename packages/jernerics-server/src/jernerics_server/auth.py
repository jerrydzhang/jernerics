import grpc


class ApiKeyInterceptor(grpc.ServerInterceptor):
    def __init__(self, expected_key: str) -> None:
        self._expected_key = expected_key

    def intercept_service(self, continuation, handler_call_details):
        metadata = dict(handler_call_details.invocation_metadata)
        if metadata.get("x-api-key") != self._expected_key:
            return grpc.unary_unary_rpc_method_handler(
                lambda req, ctx: ctx.abort(
                    grpc.StatusCode.UNAUTHENTICATED,
                    "Invalid or missing API key",
                )
            )
        return continuation(handler_call_details)
