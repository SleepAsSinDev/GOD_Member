from fastapi import FastAPI, HTTPException
from firebase_admin import credentials, initialize_app, firestore
import os
from typing import Optional, List
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import json

load_dotenv()

# --- Firebase Initialization ---
try:
    # ดึงข้อมูล JSON จากตัวแปร environment
    credentials_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if not credentials_json:
        raise ValueError("FIREBASE_CREDENTIALS_JSON is not set in .env")
    
    # Parse the JSON string, handling potential errors
    try:
        cred_dict = json.loads(credentials_json)
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
        print(f"Problematic JSON string: {credentials_json}")
        raise ValueError("Invalid JSON format in FIREBASE_CREDENTIALS_JSON") from e

    # สร้าง credentials object จาก dictionary
    cred = credentials.Certificate(cred_dict)
    initialize_app(cred)

    # ได้ reference ไปยัง Cloud Firestore database
    db = firestore.client()
    print("Firebase initialized successfully!")

except Exception as e:
    print(f"Error initializing Firebase: {e}")
    exit(1)

# --- FastAPI App Initialization ---
app = FastAPI()

# --- Simple Test Endpoint ---
@app.get("/")
async def read_root():
    return {"message": "Welcome to the FastAPI Firebase API!", "firebase_status": "connected"}

# คุณสามารถทดสอบว่า Firebase DB เชื่อมต่อได้หรือไม่จาก endpoint นี้
@app.get("/test_firestore_connection")
async def test_firestore_connection():
    try:
        # ลองดึงข้อมูลจาก collection ที่ไม่มีอยู่จริง เพื่อทดสอบว่าเชื่อมต่อได้
        doc_ref = db.collection('test_collection').document('test_doc')
        doc_ref.get() # แค่ลองเรียก get() เพื่อดูว่าไม่มี error จากการเชื่อมต่อ
        return {"message": "Firestore connection successful!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Firestore connection failed: {e}")
    
# Model สำหรับข้อมูลลูกค้าที่ส่งเข้ามาเพื่อสร้างใหม่ (หรืออัปเดตบางส่วน)
class CustomerBase(BaseModel):
    name: str = Field(..., description="ชื่อลูกค้า")
    customer_id: Optional[str] = Field(None, description="ID ที่ต้องการใช้สำหรับ Firebase Document (ถ้ามี)")

# Model สำหรับข้อมูลลูกค้าที่ครบถ้วน ซึ่งจะส่งกลับไปให้ผู้ใช้ดู
# รวมถึงข้อมูลที่ Firestore สร้างให้ เช่น ID ของ Document
class Customer(CustomerBase): # สืบทอด name, custom_doc_id
    customer_id: str = Field(..., description="รหัสลูกค้า (เหมือนกับ Firebase Document ID)") # <--- ตรงนี้จะใช้ค่าเดียวกันกับ Document ID
    total_top_up: float = Field(0.0, description="รวมเงินที่ลูกค้าเติมเข้ามา")
    total_spent: float = Field(0.0, description="รวมเงินที่ลูกค้าใช้ไป")
    balance: float = Field(0.0, description="ยอดเงินคงเหลือของลูกค้า")

# Model สำหรับการเติมเงินหรือใช้เงิน (รับแค่จำนวนเงิน)
class TransactionAmount(BaseModel):
    amount: float = Field(..., gt=0, description="จำนวนเงิน ต้องเป็นบวก") # gt=0 คือมากกว่า 0

def _generate_sequential_customer_id(transaction):
    counter_ref = db.collection('counters').document('customer_id_counter')
    counter_doc = counter_ref.get(transaction=transaction)

    if not counter_doc.exists:
        # แก้ไขตรงนี้: ใช้ transaction.set() แทน counter_ref.set()
        transaction.set(counter_ref, {'current_value': 0}) 
        current_counter_value = 0
    else:
        current_counter_value = counter_doc.get('current_value') or 0

    new_counter_value = current_counter_value + 1
    transaction.update(counter_ref, {'current_value': new_counter_value})

    # จัดรูปแบบรหัสลูกค้า เช่น CUS-0001, CUS-0002
    generated_id = f"GOD-{new_counter_value:04d}" # ใช้ 4 หลักนำหน้าด้วย 0
    return generated_id

# --- API Endpoints ---

# Endpoint 1: Create (สร้างลูกค้าใหม่)
@app.post("/customers/", response_model=Customer, status_code=201)
async def create_customer(customer_data: CustomerBase):
    transaction = db.transaction()

    @firestore.transactional
    def create_customer_transaction(transaction, customer_data):
        doc_id_to_use = customer_data.customer_id # รับ ID ที่ผู้ใช้กำหนดมา (ถ้ามี)
        final_assigned_id = None # กำหนดค่าเริ่มต้น

        if doc_id_to_use:
            # กรณีผู้ใช้กำหนด ID เอง
            doc_ref = db.collection('customers').document(doc_id_to_use)
            # ตรวจสอบว่า ID ที่กำหนดมามีอยู่แล้วหรือไม่ (เป็นการอ่านครั้งแรกของ doc_ref)
            if doc_ref.get(transaction=transaction).exists:
                raise HTTPException(status_code=409, detail=f"Document with ID '{doc_id_to_use}' already exists. Please choose another ID.")
            final_assigned_id = doc_id_to_use

        else:
            # กรณีผู้ใช้ไม่ได้กำหนด ID เอง ให้สร้างแบบ Sequential
            # การเรียกฟังก์ชันนี้จะมีการอ่านและเขียนไปยัง Document 'counters' ภายใน Transaction เดียวกัน
            final_assigned_id = _generate_sequential_customer_id(transaction)
            doc_ref = db.collection('customers').document(final_assigned_id)

        # สร้างข้อมูลลูกค้าสำหรับบันทึก
        new_customer_data_dict = {
        "name": customer_data.name,
        "customer_id": final_assigned_id, # ใช้ ID ที่ได้มาเป็นค่าในฟิลด์ 'customer_id' ด้วย
        "total_top_up": 0.0,
        "total_spent": 0.0,
        "balance": 0.0
        }

        # บันทึก Document ด้วย ID ที่กำหนด/สร้าง
        transaction.set(doc_ref, new_customer_data_dict)

        return Customer(id=doc_ref.id, **new_customer_data_dict)

    try:
        created_customer = create_customer_transaction(transaction, customer_data)
        return created_customer
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating customer: {e}")
    
# Endpoint 2: Read (อ่านข้อมูลลูกค้าทั้งหมด)
@app.get("/customers/", response_model=List[Customer])
async def get_all_customers():
    try:
        customers_ref = db.collection('customers')
        docs = customers_ref.stream() # ดึงข้อมูลทั้งหมดเป็น stream
        
        customer_list = []
        for doc in docs:
            customer_list.append(Customer(id=doc.id, **doc.to_dict()))
        
        return customer_list
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving all customers: {e}")


# Endpoint 3: Read (อ่านข้อมูลลูกค้าตาม ID)
@app.get("/customers/{customer_id}", response_model=Customer)
async def get_customer(customer_id: str):
    try:
        customer_ref = db.collection('customers').document(customer_id)
        doc = customer_ref.get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Customer not found")
        
        return Customer(id=doc.id, **doc.to_dict())
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving customer: {e}")


# Endpoint 4: Update (เติมเงินให้ลูกค้า) - ใช้ Transaction เพื่อความปลอดภัยของข้อมูลการเงิน
@app.put("/customers/{customer_id}/topup", response_model=Customer)
async def top_up_customer_balance(customer_id: str, transaction_data: TransactionAmount):
    amount_to_add = transaction_data.amount

    transaction = db.transaction()
    customer_ref = db.collection('customers').document(customer_id)

    @firestore.transactional
    def update_in_transaction(transaction, customer_ref, amount):
        snapshot = customer_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise HTTPException(status_code=404, detail="Customer not found")

        # ดึงข้อมูลปัจจุบัน
        customer_data = snapshot.to_dict()
        current_total_top_up = customer_data.get('total_top_up', 0.0)
        current_balance = customer_data.get('balance', 0.0)

        # คำนวณค่าใหม่
        new_total_top_up = current_total_top_up + amount
        new_balance = current_balance + amount

        # อัปเดตใน Transaction
        transaction.update(customer_ref, {
            'total_top_up': new_total_top_up,
            'balance': new_balance
        })
        
        # <<< บรรทัดนี้ถูกลบออก
        # updated_doc = customer_ref.get(transaction=transaction) 
        # <<< และแทนที่ด้วยการสร้าง Customer Object จากข้อมูลที่เรามีอยู่แล้ว

        # สร้าง Customer Object ด้วยข้อมูลเดิม + ค่าที่เพิ่งอัปเดตไป
        # โดยใช้ข้อมูลจาก snapshot เป็นพื้นฐาน แล้วแทนที่ค่าที่เปลี่ยนไป
        customer_data['total_top_up'] = new_total_top_up
        customer_data['balance'] = new_balance
        
        return Customer(id=snapshot.id, **customer_data) # ใช้ snapshot.id และข้อมูลที่อัปเดตแล้ว

    try:
        updated_customer = update_in_transaction(transaction, customer_ref, amount_to_add)
        return updated_customer
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error topping up balance: {e}")


# Endpoint 5: Update (ลูกค้าใช้เงิน) - ใช้ Transaction และตรวจสอบยอดเงิน
@app.put("/customers/{customer_id}/spend", response_model=Customer)
async def spend_customer_balance(customer_id: str, transaction_data: TransactionAmount):
    amount_to_spend = transaction_data.amount

    transaction = db.transaction()
    customer_ref = db.collection('customers').document(customer_id)

    @firestore.transactional
    def update_in_transaction(transaction, customer_ref, amount):
        snapshot = customer_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise HTTPException(status_code=404, detail="Customer not found")

        # ดึงข้อมูลปัจจุบัน
        customer_data = snapshot.to_dict()
        current_total_spent = customer_data.get('total_spent', 0.0)
        current_balance = customer_data.get('balance', 0.0)

        if current_balance < amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")

        # คำนวณค่าใหม่
        new_total_spent = current_total_spent + amount
        new_balance = current_balance - amount

        # อัปเดตใน Transaction
        transaction.update(customer_ref, {
            'total_spent': new_total_spent,
            'balance': new_balance
        })

        # สร้าง Customer Object ด้วยข้อมูลเดิม + ค่าที่เพิ่งอัปเดตไป
        customer_data['total_spent'] = new_total_spent
        customer_data['balance'] = new_balance
        
        return Customer(id=snapshot.id, **customer_data) # ใช้ snapshot.id และข้อมูลที่อัปเดตแล้ว

    try:
        updated_customer = update_in_transaction(transaction, customer_ref, amount_to_spend)
        return updated_customer
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error spending balance: {e}")


# Endpoint 6: Delete (ลบข้อมูลลูกค้า)
@app.delete("/customers/{customer_id}", status_code=204)
async def delete_customer(customer_id: str):
    try:
        customer_ref = db.collection('customers').document(customer_id)
        doc = customer_ref.get()

        if not doc.exists:
            raise HTTPException(status_code=404, detail="Customer not found")

        customer_ref.delete()
        return # No content for 204 status code
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting customer: {e}")