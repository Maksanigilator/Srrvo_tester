#include <Arduino.h>
#include <SCServo.h>


const uint8_t Servo_RX = 18;
const uint8_t Servo_TX = 19; 
SMS_STS st;

void setup(){
    Serial.begin(115200);

    Serial1.begin(1000000, SERIAL_8N1, Servo_RX, Servo_TX);
    st.pSerial = &Serial1; 
}

void loop(){

}