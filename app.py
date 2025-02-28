from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_migrate import Migrate
import os
import requests
import datetime
import base64
from models import db, Order, Payment

def create_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URI', 'sqlite:///NewHaven.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)
    Migrate(app, db)
    CORS(app, supports_credentials=True)
    return app

app = create_app()

CONSUMER_KEY = os.environ.get("CONSUMER_KEY", "3IaIdasO6qz8DC7WvXQOg22ezqR7UxBBUv9ERSZMddXLdDC9")
CONSUMER_SECRET = os.environ.get("CONSUMER_SECRET", "FWNl7M44MWBMcfuL5YRRAGq3dsDELYjgJY2NuyGDhCOlROxMPmr0olWOmaf0uSq4")
SHORTCODE = os.environ.get("BUSINESS_SHORT_CODE", "656025")
PASSKEY = os.environ.get("PASSKEY", "eec68e6145cec58fe21712a9bffc1966addc9fbd5e3e3e021ffdb92573a88a68")
CALLBACK_URL = os.environ.get("CALLBACK_URL", "https://havenplacebackend-2.onrender.com/stk/callback")

# Production Mpesa Express Endpoints
TOKEN_URL = "https://api.safaricom.co.ke/oauth/v1/generate"
STK_PUSH_URL = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
STK_PUSH_QUERY_URL = "https://api.safaricom.co.ke/mpesa/stkpushquery/v1/query"  # For potential future use

def get_access_token():
    """Obtain a fresh access token from Mpesa using production credentials."""
    try:
        url = f"{TOKEN_URL}?grant_type=client_credentials"
        headers = {"Accept": "application/json"}
        response = requests.get(
            url,
            headers=headers,
            auth=requests.auth.HTTPBasicAuth(CONSUMER_KEY, CONSUMER_SECRET),
            timeout=10
        )
        app.logger.debug("Access Token Response Code: %s", response.status_code)
        app.logger.debug("Access Token Response Text: %s", response.text)
        if response.status_code != 200:
            app.logger.error("Failed to obtain access token. Status Code: %s. Response: %s",
                             response.status_code, response.text)
            return None
        data = response.json()
        access_token = data.get("access_token")
        if access_token:
            access_token = access_token.strip()
        else:
            app.logger.error("Access token not found in response: %s", data)
        return access_token
    except Exception as e:
        app.logger.error("Exception while fetching access token: %s", str(e))
        return None

def regenerate_access_token():
    """Helper function to force regenerating a new access token."""
    new_token = get_access_token()
    if new_token:
        app.logger.info("Access token regenerated successfully: %s", new_token)
    else:
        app.logger.error("Failed to regenerate access token.")
    return new_token

# ===========================
# Endpoints
# ===========================

@app.route('/api/payments/mpesa/myphone', methods=['POST'])
def mpesa_payment_myphone():
    """
    Initiate an Mpesa Express STK Push using production endpoints.
    Expects a JSON payload with: amount, order_id, and optionally phone_number.
    """
    try:
        data = request.get_json(force=True) or {}
        amount = data.get("amount")
        order_id = data.get("order_id", "MYORDER")  # Default order reference if not provided

        if not amount:
            return jsonify({"error": "Missing amount"}), 400

        try:
            amount = float(amount)
        except ValueError:
            return jsonify({"error": "Amount must be a number."}), 400

        phone = data.get("phone_number") or os.environ.get("MY_MPESA_PHONE", "254722880230")
        access_token = get_access_token()
        if not access_token:
            return jsonify({"error": "Failed to obtain access token"}), 500

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        raw_password = f"{SHORTCODE}{PASSKEY}{timestamp}"
        encoded_password = base64.b64encode(raw_password.encode()).decode()

        stk_payload = {
            "BusinessShortCode": SHORTCODE,
            "Password": encoded_password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": amount,
            "PartyA": phone,
            "PartyB": SHORTCODE,
            "PhoneNumber": phone,
            "CallBackURL": CALLBACK_URL,
            "AccountReference": "Web-Order",
            "TransactionDesc": "Payment to my phone"
        }

        saf_response = requests.post(STK_PUSH_URL, json=stk_payload, headers=headers)
        app.logger.debug("Mpesa STK push response for my phone: %s", saf_response.text)
        response_data = saf_response.json()
        if saf_response.status_code == 200 and response_data.get("ResponseCode") == "0":
            # Create and save a new Payment record with status 'pending'
            new_payment = Payment(
                order_id=order_id,
                transaction_id=response_data.get("CheckoutRequestID"),
                amount=amount,
                status="pending"
            )
            db.session.add(new_payment)
            db.session.commit()

        return jsonify(response_data), saf_response.status_code

    except Exception as e:
        app.logger.error("Error in mpesa_payment_myphone: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route('/stk/callback', methods=['POST'])
def stk_callback():
    """
    Handle callback from Safaricom after STK push payment.
    Updates the payment status based on the callback data.
    """
    try:
        callback_data = request.get_json()
        app.logger.info("Received STK Callback: %s", callback_data)
        if callback_data.get("Body") and callback_data["Body"].get("stkCallback"):
            stk_callback = callback_data["Body"]["stkCallback"]
            checkout_request_id = stk_callback.get("CheckoutRequestID")
            result_code = stk_callback.get("ResultCode")
            # Update the corresponding payment record based on the CheckoutRequestID
            payment = Payment.query.filter_by(transaction_id=checkout_request_id).first()
            if payment:
                if result_code == 0:
                    payment.status = "Completed"
                else:
                    payment.status = "Failed"
                db.session.commit()
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200
    except Exception as e:
        app.logger.error("Error in STK callback: %s", e)
        return jsonify({"ResultCode": 1, "ResultDesc": str(e)}), 500

@app.route('/api/payments/status', methods=['GET'])
def payment_status():
    """
    Check the status of a payment using the CheckoutRequestID.
    The frontend polls this endpoint until the status is "Completed" (or "Failed").
    """
    checkout_request_id = request.args.get("checkoutRequestID")
    if not checkout_request_id:
        return jsonify({"error": "Missing checkoutRequestID"}), 400

    payment = Payment.query.filter_by(transaction_id=checkout_request_id).first()
    if not payment:
        return jsonify({"error": "Payment not found."}), 404

    return jsonify({"status": payment.status}), 200

@app.route('/confirm_payment', methods=['POST'])
def confirm_payment():
    """
    Confirm an order by matching the order_id and Mpesa checkout reference.
    Once confirmed, the order and payment statuses are updated.
    This endpoint can be used by the frontend to verify that payment was successful
    before redirecting the user to WhatsApp.
    """
    try:
        data = request.get_json()
        order_id = data.get("order_id")
        mpesa_reference = data.get("mpesa_reference")
        if not order_id or not mpesa_reference:
            return jsonify({"error": "Missing order_id or mpesa_reference."}), 400

        order = Order.query.get(order_id)
        if not order:
            return jsonify({"error": "Order not found."}), 404

        payment = Payment.query.filter_by(order_id=order_id, transaction_id=mpesa_reference).first()
        if not payment:
            return jsonify({"error": "Payment record not found."}), 404

        if order.status == "confirmed":
            return jsonify({"message": "Order already confirmed."}), 200

        order.status = "confirmed"
        payment.status = "Completed"
        db.session.commit()
        return jsonify({"message": "Payment confirmed. Order finalized.", "order_id": order.id}), 200

    except Exception as e:
        app.logger.error("Error in confirm_payment: %s", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
