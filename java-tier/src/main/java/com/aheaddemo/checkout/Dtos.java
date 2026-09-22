package com.aheaddemo.checkout;

import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.util.List;

/**
 * Jackson-friendly DTOs shared across the controllers.
 *
 * {@link PricingResponse} is the exact JSON contract with the pricing-python tier:
 * field names here must stay camelCase and identical to the keys returned by
 * GET /api/pricing/{sku} on that service.
 */
public class Dtos {

    public record OrderRequest(Integer customerId, String sku, Integer qty) {}

    public record OrderResponse(
            Long id,
            Integer customerId,
            String customerName,
            String sku,
            Integer qty,
            BigDecimal unitPrice,
            BigDecimal totalPrice,
            String status,
            OffsetDateTime createdAt) {}

    public record OrderDetail(OrderResponse order, List<OrderEvent> events) {}

    /** Must exactly match the Python pricing-tier JSON keys. */
    public record PricingResponse(
            String sku,
            String name,
            String category,
            double unitPrice,
            double totalPrice,
            double discountPct,
            int qty,
            boolean inStock) {}

    public record OrderEvent(Long id, Long orderId, String eventType, String detail, OffsetDateTime createdAt) {}

    public record FaultState(String mode, double rate, int slowMs) {}

    public record RevenueRow(String category, BigDecimal revenue, long orderCount) {}

    public record CustomerRow(int id, String name, String tier) {}

    public record ApiError(String error) {}
}
