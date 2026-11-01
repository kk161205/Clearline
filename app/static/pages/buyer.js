const ITEM_ID = "brand-kit";
const result = document.getElementById("result");
const show = (text, cls = "") => { result.textContent = text; result.className = cls; };

// Replace the button with a confirmation and keep the buyer-link token in the URL, so the
// paid state survives a refresh or coming back to the tab (without the seller's timeline).
function showPaid(orderRef, buyerLink) {
  document.querySelector("paypal-button")?.setAttribute("hidden", "");
  document.getElementById("paid-order").textContent = `…${orderRef}`;
  document.getElementById("paid-help").href = buyerLink;
  document.getElementById("paid").hidden = false;
  show("");
  const receipt = buyerLink.split("/").pop();
  history.replaceState(null, "", `/?receipt=${encodeURIComponent(receipt)}`);
}

async function restorePaidState() {
  const receipt = new URLSearchParams(location.search).get("receipt");
  if (!receipt) return false;
  const res = await fetch(`/api/o/${encodeURIComponent(receipt)}/status`);
  if (!res.ok) return false;
  const status = await res.json();
  if (!status.paid) return false;
  showPaid(status.orderRef, status.buyerLink);
  return true;
}

async function createOrder() {
  const res = await fetch("/api/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item_id: ITEM_ID }),
  });
  if (!res.ok) throw new Error("Could not create order");
  return res.json(); // must resolve to { orderId }
}

async function init() {
  const config = await (await fetch("/api/config")).json();
  const item = config.catalogue[ITEM_ID];
  document.getElementById("item-name").textContent = item.description;
  document.getElementById("item-price").textContent = `${item.amount} ${item.currency}`;
  if (await restorePaidState()) return; // already paid: don't load the button

  const script = document.createElement("script");
  script.src = config.sdkUrl;
  script.onload = async () => {
    const sdk = await window.paypal.createInstance({
      clientId: config.clientId,
      components: ["paypal-payments"],
      pageType: "checkout",
    });
    const button = document.querySelector("paypal-button");
    button.addEventListener("click", async () => {
      const session = sdk.createPayPalOneTimePaymentSession({
        onApprove: async (data) => {
          show("Confirming your payment…");
          const res = await fetch(`/api/orders/${data.orderId}/capture`, { method: "POST" });
          const body = await res.json();
          if (res.ok && body.status === "COMPLETED") {
            showPaid(data.orderId.slice(-6), body.buyerLink);
          } else {
            show(`The payment didn't complete. Details: ${JSON.stringify(body)}`, "bad");
          }
        },
        onCancel: () => show("Payment cancelled. You haven't been charged."),
        onError: (err) => show(`PayPal error: ${err?.message || err}`, "bad"),
      });
      try {
        await session.start({ presentationMode: "auto" }, createOrder());
      } catch (err) {
        show(err.message, "bad");
      }
    });
    button.removeAttribute("hidden");
  };
  script.onerror = () => show("Could not load PayPal checkout. Please refresh the page.", "bad");
  document.head.appendChild(script);
}

init().catch((err) => show(err.message, "bad"));
