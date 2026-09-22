package com.aheaddemo.checkout;

import com.aheaddemo.checkout.Dtos.FaultState;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import java.util.Map;

/**
 * Fault-injection control surface. Same none|slow|error|dbslow contract as the
 * identical endpoint on the pricing-python tier, so a demo can drive both tiers
 * from one script.
 */
@RestController
@RequestMapping("/api/admin/fault")
public class AdminController {

    private final FaultConfig faultConfig;

    public AdminController(FaultConfig faultConfig) {
        this.faultConfig = faultConfig;
    }

    @GetMapping
    public FaultState getFault() {
        return new FaultState(faultConfig.getMode(), faultConfig.getRate(), faultConfig.getSlowMs());
    }

    @PostMapping
    public FaultState setFault(@RequestBody Map<String, Object> body) {
        Object modeObj = body.get("mode");
        String mode = modeObj != null ? modeObj.toString() : null;
        if (mode != null && !FaultConfig.VALID_MODES.contains(mode)) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST,
                    "mode must be one of " + FaultConfig.VALID_MODES);
        }

        Object rateObj = body.get("rate");
        Double rate = rateObj != null ? Double.valueOf(rateObj.toString()) : null;

        Object slowMsObj = body.get("slowMs");
        Integer slowMs = slowMsObj != null ? Integer.valueOf(slowMsObj.toString()) : null;

        faultConfig.update(mode, rate, slowMs);
        return getFault();
    }
}
